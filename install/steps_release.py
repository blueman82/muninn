"""Prepare immutable releases, activate them and manage native jobs."""

from __future__ import annotations

import io
import os
import plistlib
import shutil
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from install import configedit as ce
from install import legacy_baseline as baseline
from install import lifecycle
from install.constants import (
    GIT_ENV,
    HEARTBEAT_S,
    LABEL,
    RECALL_OFF,
)
from install.context import (
    Ctx,
    StepFailedError,
    dry,
    is_new,
    job,
    must,
    wait,
)
from install.provider_paths import render_pinned
from install.record import Record
from install.release_io import publish, write_private, write_selection
from install.runtime import prepare_runtime, retired_runtimes
from muninn import platform_io
from muninn.cursor_import import default_database
from muninn.obs_service import literal, powershell

PLIST_SOURCE = "current/launchd/com.muninn.plist"
HISTORY_ROOTS = (
    ("Claude", ".claude/projects", "*.jsonl"),
    ("Codex", ".codex/sessions", "rollout-*.jsonl"),
    ("Codex", ".codex/archived_sessions", "rollout-*.jsonl"),
)


def _history_found(ctx: Ctx) -> dict[str, bool]:
    """Detect provider history from paths without opening transcripts."""
    found = {"Claude": False, "Codex": False, "Cursor": False}
    for provider, relative, pattern in HISTORY_ROOTS:
        root = ctx.home / relative
        if root.is_dir() and any(
            path.is_file() and not path.is_symlink()
            for path in root.rglob(pattern)
        ):
            found[provider] = True
    cursor = default_database(ctx.home, env={})
    found["Cursor"] = cursor is not None and cursor.is_file()
    return found


def relink(link: Path, target: str | Path, ts: str) -> None:
    """Point ``link`` at ``target`` with one atomic rename.

    A temp symlink plus rename means a reader never sees the link missing
    or half-updated.

    Args:
        link: The symlink to create or replace.
        target: What it should point at.
        ts: Run timestamp, to keep the temp name unique.
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.{ts}")
    tmp.symlink_to(target)
    tmp.replace(link)


def _unpack(ctx: Ctx, tar: bytes, tmp: Path) -> None:
    """Extract a commit archive into ``tmp`` and substitute ``@HOME@``.

    Args:
        ctx: The run context.
        tar: The archive bytes produced by ``git archive``.
        tmp: An empty directory to extract into.
    """
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        # filter="data" refuses absolute paths, links out of the tree and
        # special files, so a hostile archive cannot escape ``tmp``.
        tf.extractall(tmp, filter="data")
    # Only these trees carry the placeholder. @HOME@ is substituted in this
    # extracted copy only, never in the source repo.
    for sub in ("integrations", "launchd"):
        for path in (tmp / sub).rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            if b"@HOME@" in data:
                relative = path.relative_to(tmp).as_posix()
                path.write_bytes(render_pinned(ctx, relative, data))


def prepare_release(ctx: Ctx, rec: Record) -> None:
    """Publish source before provisioning an immutable candidate runtime."""
    sha, dest = rec["sha"], ctx.lib / rec["sha"]
    if dry(ctx, f"would prepare the release and runtime at {dest}"):
        return
    if not dest.exists():
        argv = ["git", "-C", rec["repo"], "archive", "--format=tar", sha]
        tar = must(ctx, argv, env=GIT_ENV).stdout
        tmp = ctx.lib / f".{sha}.tmp-{ctx.ts}"
        platform_io.ensure_private_dir(tmp)
        _unpack(ctx, tar, tmp)
        publish(tmp, dest, replace=False, directory=True)
    python = prepare_runtime(ctx, dest)
    rec["python"]["path"] = str(python)
    rec["runtime_prepared"] = True
    if ctx.review_policy or rec.get("policy_input", {}).get("raw"):
        rec["prepared_runtime"] = baseline.tree_state(dest, links=True)


def pin(ctx: Ctx, rec: Record) -> None:
    """Activate a fully verified candidate without moving its runtime."""
    sha, dest = rec["sha"], ctx.lib / rec["sha"]
    if dry(ctx, f"would pin {sha} as {dest} and point current at it"):
        return
    if not rec.get("runtime_prepared"):
        prepare_release(ctx, rec)
    python = rec["python"]["path"]
    if ctx.platform == "win32":
        platform_io.ensure_private_dir(ctx.muninn.parent)
        for name in ("muninn.cmd", "muninn.ps1"):
            write_private(
                ctx.muninn.parent / name, (dest / "bin" / name).read_bytes()
            )
        write_selection(ctx.lib, sha, python)
        return
    relink(ctx.lib / "current", sha, ctx.ts)
    if "legacy" not in rec:
        relink(ctx.muninn, ctx.lib / "current/bin/muninn", ctx.ts)
    relink(ctx.lib / "python", python, ctx.ts)


def muninn_env(home: Path, **extra: str) -> dict[str, str]:
    """Build the environment for running muninn.

    Args:
        home: The data directory, passed as ``MUNINN_HOME``.
        **extra: Further variables to set.

    Returns:
        The current environment without any inherited ``MUNINN_*`` value (a
        stray ``MUNINN_ROOTS`` override would point muninn at the wrong data),
        plus ``MUNINN_HOME`` and ``extra``.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("MUNINN_")}
    return dict(env, MUNINN_HOME=str(home), **extra)


def fresh(ctx: Ctx, since: float, previous: int | None = None) -> bool:
    """Say whether the job wrote a heartbeat after ``since``.

    Args:
        ctx: The run context.
        since: Timestamp the heartbeat must not predate by more than one
            second.
        previous: Pre-start modification time that cannot prove service work.

    Returns:
        True when status.json was written at most one second before
        ``since`` or later, and is less than ``HEARTBEAT_S`` old.
    """
    try:
        status = (ctx.data / "status.json").stat()
    except FileNotFoundError:
        return False
    # One second of slack: filesystem mtimes can be coarser than our clock.
    return (
        status.st_mtime_ns != previous
        and status.st_mtime >= since - 1
        and ctx.now() - status.st_mtime < HEARTBEAT_S
    )


def ingest_fresh(ctx: Ctx, rec: Record) -> None:
    """Report completed fresh indexing and initialize the recall switch.

    Also creates ``recall.off`` so per-prompt recall starts off, and says how
    to turn it on; only a fresh install does, never an upgrade.

    ``doctor`` runs in verify, once the launchd job is up.

    Raises:
        StepFailedError: If the completed ingest lacks provider event counts.
    """
    found = _history_found(ctx)
    ctx.say("Conversation history:")
    for provider, detected in found.items():
        status = (
            "found; would index"
            if detected and ctx.dry_run
            else "found; indexed" if detected else "no history found"
        )
        ctx.say(f"  {provider}: {status}")
    if dry(
        ctx,
        f"would create {ctx.data}, start with recall off and index "
        "existing transcripts",
    ):
        return
    platform_io.ensure_private_dir(ctx.data)
    flag = ctx.data / RECALL_OFF
    # Mode 0600 from creation: doctor fails file_modes on a looser file.
    os.close(
        platform_io.open_private(flag, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    )
    ctx.say(
        "Per-prompt recall starts OFF: muninn will not add earlier prompts to "
        "your prompts until you turn it on. SessionStart memory is "
        "unaffected. Why off: a pre-release trial answered for the wrong "
        "project, so recall waits for a re-check (docs/adr/0007). "
        f"To turn recall on: unlink {flag}"
    )
    raw_counts: object = rec.get("events_by_provider")
    if not isinstance(raw_counts, dict):
        raise StepFailedError(
            "fresh preparation omitted provider event counts"
        )
    counts = cast(dict[str, object], raw_counts)
    totals: list[str] = []
    for provider in ("Claude", "Codex", "Cursor"):
        count: object = counts.get(provider.lower(), 0)
        if not isinstance(count, int) or isinstance(count, bool):
            raise StepFailedError(
                "fresh preparation returned invalid provider counts"
            )
        noun = "event" if count == 1 else "events"
        totals.append(f"{provider} {count} {noun}")
    ctx.say(f"History indexing complete: {' · '.join(totals)}.")


def history_upgrade(ctx: Ctx, _rec: Record) -> None:
    """Explain the history behavior during an upgrade.

    Args:
        ctx: The run context.
        _rec: The install record, unused by this informational step.
    """
    if ctx.dry_run:
        ctx.say(
            "History import: would be skipped during upgrade; existing index "
            "would be retained."
        )
        ctx.say(
            "Claude and Codex polling would resume after a successful "
            "upgrade; Cursor would not be re-imported."
        )
        return
    ctx.say("History import: skipped during upgrade; existing index retained.")
    ctx.say(
        "Claude and Codex polling resumes after a successful upgrade; Cursor "
        "is not re-imported."
    )


def restart(ctx: Ctx, rec: Record, *, registered: bool = False) -> None:
    """Kickstart the running job so it runs the re-pinned release."""
    if dry(ctx, f"would restart the poller ({ctx.target})"):
        return
    started = ctx.now()
    if ctx.platform != "darwin":
        lifecycle.start(ctx, Path(rec["python"]["path"]), ctx.release)
    elif rec.get("paused") and not registered:
        must(ctx, ["launchctl", "bootstrap", f"gui/{ctx.uid}", ctx.plist])
    else:
        must(ctx, ["launchctl", "kickstart", "-k", ctx.target])
    wait(ctx, lambda: is_new(ctx, job(ctx)), 30, "job has no live PID")
    wait(ctx, lambda: fresh(ctx, started), HEARTBEAT_S, "heartbeat not fresh")


PRUNING = ".pruning-"


def prune(ctx: Ctx, rec: Record) -> None:
    """Move every release dir but the one being installed out of the way.

    Legacy releases move aside reversibly. Provisioned runtimes stay at
    their immutable paths until ``sweep`` deletes them after rollback ends.

    Args:
        ctx: The run context.
        rec: The run record.

    Raises:
        OSError: If a rename fails, after undoing the ones already done
            (the original error is the one raised, even when an undo
            fails too; the dirs left behind are named in the output).
    """
    keep = rec["sha"]
    old = [
        p
        for p in sorted(ctx.lib.iterdir() if ctx.lib.is_dir() else [])
        if p.is_dir()
        and not p.is_symlink()
        and p.name != keep
        and not p.name.startswith(PRUNING)
    ]
    if ctx.dry_run:
        for p in old:
            if (p / ".venv").exists():
                dry(
                    ctx,
                    f"would delete the previous release {p.name} in place "
                    "once the install succeeds (no way back)",
                )
                continue
            dry(
                ctx,
                f"would move the previous release {p.name} aside, then delete"
                " it once the install succeeds (no way back)",
            )
        return
    old = [p for p in old if not (p / ".venv").exists()]
    moved: list[tuple[Path, Path]] = []
    try:
        for path in old:
            gone = path.with_name(f"{PRUNING}{ctx.ts}-{path.name}")
            path.rename(gone)
            moved.append((gone, path))
    except OSError:
        stuck: list[str] = []
        for gone, path in reversed(moved):
            try:
                gone.rename(path)
            except OSError:  # keep undoing the rest
                stuck.append(gone.name)
        if stuck:
            ctx.say(
                f"could not move back {', '.join(stuck)} in {ctx.lib};"
                " rename each to the name after its .pruning-<time>- prefix"
            )
        raise


def sweep(ctx: Ctx) -> list[str]:
    """Delete the dirs ``prune`` moved aside, and any a past run left.

    A failure is only a warning: the install already succeeded and the
    next upgrade retries.  ``muninn doctor`` also warns while one is left.

    Args:
        ctx: The run context.

    Returns:
        The names of the dirs that could not be deleted.
    """
    if ctx.dry_run or not ctx.lib.is_dir():
        return []
    stuck: list[str] = []
    stale = [*ctx.lib.glob(f"{PRUNING}*"), *retired_runtimes(ctx)]
    for path in sorted(stale):
        try:
            shutil.rmtree(path)
        except OSError as exc:
            stuck.append(path.name)
            ctx.say(f"could not delete {path}: {exc}; remove it by hand")
    return stuck


def _check_plist(ctx: Ctx, data: bytes) -> None:
    """Refuse a pinned plist that is not ready to bootstrap.

    Args:
        ctx: The run context.
        data: The pinned plist bytes.

    Raises:
        StepFailedError: If a placeholder is left, the label is wrong, or it
            does not run the pinned ``bin/muninn``.
    """
    prog = f"{ctx.lib}/current/bin/muninn"
    plist = plistlib.loads(data)
    if b"@HOME@" in data or plist.get("Label") != LABEL:
        raise StepFailedError(
            "pinned plist is not substituted or has another label"
        )
    if plist["ProgramArguments"][0] != prog or not os.access(prog, os.X_OK):
        raise StepFailedError("pinned plist does not run current/bin/muninn")


def start_new(ctx: Ctx, rec: Record) -> None:
    """Install the pinned plist, bootstrap, and await PID and heartbeat."""
    src = ctx.lib / PLIST_SOURCE
    if dry(ctx, f"would install the launchd job {ctx.plist} and start it"):
        return
    status = ctx.data / "status.json"
    previous = status.stat().st_mtime_ns if status.exists() else None
    if ctx.platform != "darwin":
        started = ctx.now()
        lifecycle.start(ctx, Path(rec["python"]["path"]), ctx.release)
        wait(ctx, lambda: is_new(ctx, job(ctx)), 30, "new job has no live PID")
        wait(
            ctx,
            lambda: fresh(ctx, started, previous),
            HEARTBEAT_S,
            "heartbeat not fresh",
        )
        return
    data = src.read_bytes()
    _check_plist(ctx, data)
    ctx.plist.parent.mkdir(parents=True, exist_ok=True)
    ce.atomic_write(ctx.plist, data, 0o644)
    started = ctx.now()
    must(ctx, ["launchctl", "bootstrap", f"gui/{ctx.uid}", ctx.plist])
    wait(ctx, lambda: is_new(ctx, job(ctx)), 30, "new job has no live PID")
    wait(
        ctx,
        lambda: fresh(ctx, started, previous),
        HEARTBEAT_S,
        "heartbeat not fresh in 120 s",
    )


def command(ctx: Ctx, *args: str) -> Sequence[str | Path]:
    """Invoke installed Windows code directly; retain the POSIX launcher."""
    if ctx.platform == "win32":
        script = f"$launcher={literal(str(ctx.muninn.with_suffix('.ps1')))};"
        encoded_args = ",".join(f"({literal(value)})" for value in args)
        script += (
            f"$values=@({encoded_args});& $launcher @values;exit $LASTEXITCODE"
        )
        argv = powershell(script)
        argv[3:3] = ["-ExecutionPolicy", "Bypass"]
        return argv
    return [ctx.muninn, *args]


def pause(ctx: Ctx, rec: Record) -> None:
    """Stop the managed native writer before any upgrade mutation."""
    if dry(ctx, "would pause the owned writer before upgrade"):
        return
    lifecycle.stop(ctx)
    rec["paused"] = True


def installed_env(ctx: Ctx, **extra: str) -> dict[str, str]:
    """Isolate provider configuration as well as the Muninn data directory."""
    extra = dict(extra)
    if ctx.platform == "win32":
        extra["LOCALAPPDATA"] = str(ctx.lib.parent.parent)
    return muninn_env(
        ctx.data,
        HOME=str(ctx.home),
        USERPROFILE=str(ctx.home),
        CODEX_HOME=str(ctx.codex_home),
        CLAUDE_CONFIG_DIR=str(ctx.settings.parent),
        **extra,
    )
