"""Install steps that manage the pinned release and the launchd job.

``pin`` unpacks the commit into ``lib/<sha>`` and relinks; ``restart`` and
``start_new`` bring the job up on it; ``prune`` removes superseded releases.
"""

from __future__ import annotations

import io
import os
import plistlib
import shutil
import sys
import tarfile
from pathlib import Path

from install import configedit as ce
from install.constants import GIT_ENV, HEARTBEAT_S, LABEL, RECALL_OFF
from install.context import (
    Ctx,
    StepFailedError,
    dry,
    is_new,
    job,
    must,
    wait,
)
from install.record import Record

PLIST_SOURCE = "current/launchd/com.provenance-context.plist"


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
    home = str(ctx.home).encode()
    # Only these trees carry the placeholder. @HOME@ is substituted in this
    # extracted copy only, never in the source repo.
    for sub in ("integrations", "launchd"):
        for path in (tmp / sub).rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            if b"@HOME@" in data:
                path.write_bytes(data.replace(b"@HOME@", home))


def pin(ctx: Ctx, rec: Record) -> None:
    """Unpack the pinned commit into lib/<sha>, fill @HOME@, and relink."""
    sha, dest = rec["sha"], ctx.lib / rec["sha"]
    if dry(ctx, f"would pin {sha} as {dest} and point current at it"):
        return
    argv = ["git", "-C", rec["repo"], "archive", "--format=tar", sha]
    # Archive first: a failure here must not leave a half-made temp dir.
    tar = must(ctx, argv, env=GIT_ENV).stdout
    tmp = ctx.lib / f".{sha}.tmp-{ctx.ts}"
    tmp.mkdir(parents=True)
    _unpack(ctx, tar, tmp)
    if dest.exists():
        # Re-pinning the same commit: keep the old copy until prune.
        dest.rename(ctx.lib / f"{sha}.superseded-{ctx.ts}")
    tmp.rename(dest)
    relink(ctx.lib / "current", sha, ctx.ts)
    relink(ctx.pctx, ctx.lib / "current/bin/pctx", ctx.ts)
    # bin/pctx reads this link to find the interpreter that ran the install.
    relink(ctx.lib / "python", sys.executable, ctx.ts)


def pctx_env(home: Path, **extra: str) -> dict[str, str]:
    """Build the environment for running pctx.

    Args:
        home: The data directory, passed as ``PCTX_HOME``.
        **extra: Further variables to set.

    Returns:
        The current environment without any inherited ``PCTX_*`` value (a
        stray ``PCTX_ROOTS`` override would point pctx at the wrong data),
        plus ``PCTX_HOME`` and ``extra``.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PCTX_")}
    return dict(env, PCTX_HOME=str(home), **extra)


def fresh(ctx: Ctx, since: float) -> bool:
    """Say whether the job wrote a heartbeat after ``since``.

    Args:
        ctx: The run context.
        since: Timestamp the heartbeat must not predate by more than one
            second.

    Returns:
        True when status.json was written at most one second before
        ``since`` or later, and is less than ``HEARTBEAT_S`` old.
    """
    try:
        mtime = (ctx.data / "status.json").stat().st_mtime
    except FileNotFoundError:
        return False
    # One second of slack: filesystem mtimes can be coarser than our clock.
    return mtime >= since - 1 and ctx.now() - mtime < HEARTBEAT_S


def ingest_fresh(ctx: Ctx, rec: Record) -> None:
    """Create the data dir and index what already exists (fresh install).

    Also creates ``recall.off`` so per-prompt recall starts off, and says how
    to turn it on; only a fresh install does, never an upgrade.

    ``doctor`` runs in verify, once the launchd job is up.
    """
    if dry(
        ctx,
        f"would create {ctx.data}, start with recall off and index "
        "existing transcripts",
    ):
        return
    ctx.data.mkdir(parents=True, mode=0o700)
    flag = ctx.data / RECALL_OFF
    # Mode 0600 from creation: doctor fails file_modes on a looser file.
    os.close(os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    ctx.say(
        "Per-prompt recall starts OFF: pctx will not add earlier prompts to "
        "your prompts until you turn it on. SessionStart memory is "
        "unaffected. Why off: a pre-release trial answered for the wrong "
        "project, so recall waits for a re-check (docs/adr/0007). "
        f"To turn recall on: unlink {flag}"
    )
    must(ctx, [ctx.pctx, "ingest", "--full"], env=pctx_env(ctx.data))


def restart(ctx: Ctx, rec: Record) -> None:
    """Kickstart the running job so it runs the re-pinned release."""
    if dry(ctx, f"would restart the poller ({ctx.target})"):
        return
    started = ctx.now()
    must(ctx, ["launchctl", "kickstart", "-k", ctx.target])
    wait(ctx, lambda: is_new(ctx, job(ctx)), 30, "job has no live PID")
    wait(ctx, lambda: fresh(ctx, started), HEARTBEAT_S, "heartbeat not fresh")


def prune(ctx: Ctx, rec: Record) -> None:
    """Delete every release dir but the one being installed.

    Runs last, so a failed install can still roll back to the old release.
    A dry run has not pinned yet, so it keeps ``rec["sha"]``, which is what
    ``current`` points at once the real run gets here.
    """
    keep = rec["sha"]
    old = [
        p
        for p in sorted(ctx.lib.iterdir() if ctx.lib.is_dir() else [])
        if p.is_dir() and not p.is_symlink() and p.name != keep
    ]
    if ctx.dry_run:
        for p in old:
            dry(
                ctx,
                f"would delete the previous release {p.name} (no way back)",
            )
        return
    for path in old:
        shutil.rmtree(path)


def _check_plist(ctx: Ctx, data: bytes) -> None:
    """Refuse a pinned plist that is not ready to bootstrap.

    Args:
        ctx: The run context.
        data: The pinned plist bytes.

    Raises:
        StepFailedError: If a placeholder is left, the label is wrong, or it
            does not run the pinned ``bin/pctx``.
    """
    prog = f"{ctx.lib}/current/bin/pctx"
    plist = plistlib.loads(data)
    if b"@HOME@" in data or plist.get("Label") != LABEL:
        raise StepFailedError(
            "pinned plist is not substituted or has another label"
        )
    if plist["ProgramArguments"][0] != prog or not os.access(prog, os.X_OK):
        raise StepFailedError("pinned plist does not run current/bin/pctx")


def start_new(ctx: Ctx, rec: Record) -> None:
    """Install the pinned plist, bootstrap, and await PID and heartbeat."""
    src = ctx.lib / PLIST_SOURCE
    if dry(ctx, f"would install the launchd job {ctx.plist} and start it"):
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
        lambda: fresh(ctx, started),
        HEARTBEAT_S,
        "heartbeat not fresh in 120 s",
    )
