"""Undo an installer run from its rollback record.

  python3.13 -E -s -B -m install.rollback --record RECORD [--dry-run]

RECORD is rollback-record.json in
~/.local/share/muninn-install-<ts> (the installer rolls back by
itself on failure; this is for a run that was interrupted). Every action
first looks at launchd and the filesystem, so it is safe after a failure at
any step, a crash, or a second run. A successful --upgrade deletes its
record: there is no way back to the previous release other than re-pinning
an earlier commit with --upgrade.
"""

from __future__ import annotations

import argparse
import functools
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from install import configedit as ce
from install import lifecycle
from install.constants import PRIVATE_DIR_MODE, PRIVATE_UMASK
from install.context import Ctx, Job, job, link_text, must, run_real
from install.provider_paths import cursor_command
from install.record import Record, load_record
from install.release_io import write_private
from install.snapshot import start_again, undo_store
from install.steps_release import PRUNING, relink
from install.transforms import codex_check, restore_cursor_settings


def restore_json(data: bytes, entries: Sequence[Mapping[str, Any]]) -> bytes:
    """Put our JSON keys back to their recorded before-values.

    Args:
        data: The current settings file bytes.
        entries: Recorded before-values from ``record.json_entry``.

    Returns:
        The restored bytes.
    """
    obj = ce.load_json(data)
    for e in entries:
        path = tuple(e["path"])
        if e["present"]:
            ce.jset(obj, path, e["value"], e["index"])
        else:
            ce.jdel(obj, path)
        # A parent we created is removed again once it is empty.
        if not e["parent_present"] and ce.jget(obj, path[:-1])[1] == {}:
            ce.jdel(obj, path[:-1])
    return ce.dump_like(data, obj)


def restore_toml(text: str, sections: Sequence[Mapping[str, Any]]) -> str:
    """Put our config.toml sections back to their recorded blocks.

    Args:
        text: The current config.toml.
        sections: Recorded blocks from ``record.codex_record``, file order.

    Returns:
        The restored text.
    """
    for s in sections:
        text = ce.put_section(text, s["header"], None)
    # Re-insert in ascending table index: by the time a later section goes
    # back, every earlier table is already in place, so its index is valid.
    for s in sections:
        if s["raw"] is not None:
            text = ce.put_section(text, s["header"], s["raw"], at=s["at"])
    return text


def aside(ctx: Ctx, path: Path, name: str) -> None:
    """Move a new-system artefact into the failed dir (never delete)."""
    ctx.failed.mkdir(mode=PRIVATE_DIR_MODE, exist_ok=True)
    path.rename(ctx.failed / name)


def _act(ctx: Ctx, text: str, fn: Callable[[], object]) -> None:
    """Announce one rollback action and run it unless this is a dry run."""
    ctx.say(f"{'DRY-RUN ' if ctx.dry_run else ''}rollback: {text}")
    if not ctx.dry_run:
        fn()


def bootout(ctx: Ctx) -> None:
    """Unload the job and wait until launchd confirms it is gone."""
    lifecycle.stop(ctx)


def _undo_fresh(ctx: Ctx, j: Job | None) -> None:
    """Remove what a fresh install created, moving it aside, not deleting.

    Args:
        ctx: The run context.
        j: The launchd job as found at the start of the rollback.
    """
    if ctx.platform != "darwin" and ctx.plist.exists():
        _act(ctx, "remove the owned native job", lambda: lifecycle.remove(ctx))
    elif j:
        _act(ctx, f"bootout new job pid {j['pid']}", lambda: bootout(ctx))
    # Only a fresh install made these, so only then are they ours to move.
    if ctx.data.exists():
        _act(
            ctx, f"move {ctx.data} aside", lambda: aside(ctx, ctx.data, "data")
        )
    if ctx.plist.exists():
        _act(
            ctx, "move new plist aside", lambda: aside(ctx, ctx.plist, "plist")
        )


def _restore_cursor(
    ctx: Ctx,
    rec: Record,
    entries: Sequence[Mapping[str, Any]],
    paths: Sequence[ce.JsonPath],
) -> None:
    """Restore Cursor's prior hook keys and remove a newly empty config."""
    ce.edit_file(
        ctx.cursor_settings,
        lambda data: restore_cursor_settings(
            data, cursor_command(ctx), rec["cursor_ours"], entries[0]
        ),
        lambda before, after: ce.json_check(before, after, paths),
    )
    if (
        not rec.get("cursor_file_present")
        and ce.load_json(ce.read_file(ctx.cursor_settings)) == {}
    ):
        ctx.cursor_settings.unlink(missing_ok=True)


def _undo_config(ctx: Ctx, rec: Record) -> None:
    """Restore the provider config keys we changed."""
    claude = rec.get("claude", {}).get("settings")
    if claude and ctx.settings.exists():
        paths = [tuple(e["path"]) for e in claude]
        _act(
            ctx,
            f"restore {len(paths)} keys in {ctx.settings.name}",
            lambda: ce.edit_file(
                ctx.settings,
                lambda b: restore_json(b, claude),
                lambda a, b: ce.json_check(a, b, paths),
            ),
        )
    cursor = rec.get("cursor")
    if cursor and ctx.cursor_settings.exists():
        paths = [tuple(e["path"]) for e in cursor]

        _act(
            ctx,
            f"restore {len(paths)} keys in {ctx.cursor_settings.name}",
            lambda: _restore_cursor(ctx, rec, cursor, paths),
        )
    if rec.get("codex"):
        _act(
            ctx,
            f"restore {len(rec['codex'])} sections in config.toml",
            lambda: ce.edit_file(
                ctx.config,
                lambda b: restore_toml(b.decode(), rec["codex"]).encode(),
                codex_check,
            ),
        )


def _undo_links(ctx: Ctx, rec: Record) -> None:
    """Point the release links back at their recorded targets."""
    if ctx.platform == "win32":
        previous = rec.get("selection")
        manifest = ctx.lib / "selection.json"
        if previous is not None:
            _act(
                ctx,
                "restore the previous native release selection",
                lambda: write_private(manifest, previous.encode()),
            )
        elif manifest.exists():
            _act(ctx, "remove the fresh native selection", manifest.unlink)
        return
    for name, target in rec.get("links", {}).items():
        link = Path(name)
        if target is None and link.is_symlink():
            _act(ctx, f"remove {link}", link.unlink)
        elif target is not None and (
            not link.is_symlink() or link_text(link) != target
        ):
            _act(
                ctx,
                f"relink {link}",
                functools.partial(relink, link, target, ctx.ts),
            )


def _undo_prune(ctx: Ctx) -> None:
    """Give back the releases this run's ``prune`` moved aside.

    A run killed after ``prune`` and before ``sweep`` leaves the old
    release under a ``.pruning-`` name, and the links below would point at
    a name that no longer exists.
    """
    prefix = f"{PRUNING}{ctx.ts}-"
    if not ctx.lib.is_dir():
        return
    for gone in sorted(ctx.lib.glob(f"{prefix}*")):
        name = gone.name[len(prefix) :]
        if not (ctx.lib / name).exists():
            _act(
                ctx,
                f"restore release {name}",
                functools.partial(gone.rename, ctx.lib / name),
            )


def rollback(ctx: Ctx, rec: Record) -> list[str]:
    """Undo whatever the install did.

    Args:
        ctx: The run context.
        rec: The rollback record.

    Returns:
        Problems that could not be undone. Failures currently propagate as
        exceptions, so the list is empty; the signature leaves room for
        actions that should be survivable.
    """
    problems: list[str] = []
    # The copy of the store comes before anything is changed, so a failure
    # there has nothing to undo and must not restart the running job.
    if rec.get("failed", {}).get("step") in {"quiesce", "snapshot_store"}:
        return problems
    j = job(ctx)
    if ctx.platform != "darwin" and rec.get("upgrade"):
        lifecycle.stop(ctx)
    if rec.get("fresh"):
        _undo_fresh(ctx, j)
    _undo_config(ctx, rec)
    _undo_prune(ctx)
    _undo_links(ctx, rec)
    # After the relink and before any restart: the old release must find a
    # store it can open, and nothing may be writing while it is swapped.
    stopped = undo_store(ctx, rec)
    if stopped or (rec.get("upgrade") and ctx.platform != "darwin"):
        _act(ctx, "start the job on the old release", lambda: start_again(ctx))
    elif rec.get("upgrade") and job(ctx):  # back onto the old release
        kick = ["launchctl", "kickstart", "-k", ctx.target]
        _act(ctx, "restart the job", lambda: must(ctx, kick))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Roll back an interrupted run from its record.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv``.

    Returns:
        1 when problems remain, otherwise 0.
    """
    ap = argparse.ArgumentParser(prog="install.rollback")
    ap.add_argument("--record", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    rec = load_record(args.record)
    if Path(rec["home"]).resolve() != Path.home().resolve():
        ap.error("the record belongs to another HOME")
    os.umask(PRIVATE_UMASK)
    ctx = Ctx(Path(rec["home"]), run_real, rec["ts"], dry_run=args.dry_run)
    if ctx.platform != "darwin":
        lifecycle.preflight_service(ctx)
    problems = rollback(ctx, rec)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
