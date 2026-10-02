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
from install.context import Ctx, Job, job, link_text, must, run_real, wait
from install.record import Record, load_record
from install.steps_release import relink
from install.transforms import codex_check


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
    ctx.failed.mkdir(mode=0o700, exist_ok=True)
    path.rename(ctx.failed / name)


def _act(ctx: Ctx, text: str, fn: Callable[[], object]) -> None:
    """Announce one rollback action and run it unless this is a dry run."""
    ctx.say(f"{'DRY-RUN ' if ctx.dry_run else ''}rollback: {text}")
    if not ctx.dry_run:
        fn()


def _bootout(ctx: Ctx) -> None:
    """Unload the new job and wait until launchd confirms it is gone."""
    ctx.run(["launchctl", "bootout", ctx.target])
    wait(ctx, lambda: job(ctx) is None, 30, "new job still loaded")


def _undo_fresh(ctx: Ctx, j: Job | None) -> None:
    """Remove what a fresh install created, moving it aside, not deleting.

    Args:
        ctx: The run context.
        j: The launchd job as found at the start of the rollback.
    """
    if j:
        _act(ctx, f"bootout new job pid {j['pid']}", lambda: _bootout(ctx))
    # Only a fresh install made these, so only then are they ours to move.
    if ctx.data.exists():
        _act(
            ctx, f"move {ctx.data} aside", lambda: aside(ctx, ctx.data, "data")
        )
    if ctx.plist.exists():
        _act(
            ctx, "move new plist aside", lambda: aside(ctx, ctx.plist, "plist")
        )


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
    j = job(ctx)
    if rec.get("fresh"):
        _undo_fresh(ctx, j)
    _undo_config(ctx, rec)
    _undo_links(ctx, rec)
    if rec.get("upgrade") and job(ctx):  # back onto the old release
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
    os.umask(0o077)
    ctx = Ctx(Path(rec["home"]), run_real, rec["ts"], dry_run=args.dry_run)
    problems = rollback(ctx, rec)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
