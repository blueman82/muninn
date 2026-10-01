"""Undo an installer run from its rollback record.

  python3.13 -E -s -B -m install.rollback --record RECORD [--dry-run]
RECORD is rollback-record.json in ~/.local/share/provenance-context-install-<ts>
(the installer rolls back by itself on failure; this is for a run that was
interrupted). Every action first looks at launchd and the filesystem, so it is
safe after a failure at any step, a crash, or a second run. A successful
--upgrade deletes its record: there is no way back to the previous release
other than re-pinning an earlier commit with --upgrade.
"""

import argparse
import json
import os
from pathlib import Path

from install import configedit as ce
from install import installer as co


def restore_json(data, entries):
    obj = ce.load_json(data)
    for e in entries:
        path = tuple(e["path"])
        if e["present"]:
            ce.jset(obj, path, e["value"], e["index"])
        else:
            ce.jdel(obj, path)
        if not e["parent_present"] and ce.jget(obj, path[:-1])[1] == {}:
            ce.jdel(obj, path[:-1])
    return ce.dump_like(data, obj)


def restore_toml(text, sections):
    for s in sections:
        text = ce.put_section(text, s["header"], None)
    for s in sections:  # ascending table index: earlier tables are back
        if s["raw"] is not None:
            text = ce.put_section(text, s["header"], s["raw"], at=s["at"])
    return text


def aside(ctx, path, name):
    """Move a new-system artefact into the failed dir (never delete)."""
    ctx.failed.mkdir(mode=0o700, exist_ok=True)
    os.rename(path, ctx.failed / name)


def rollback(ctx, rec):
    """Undo whatever the install did; returns a list of problems."""
    problems = []

    def act(text, fn):
        ctx.say(f"{'DRY-RUN ' if ctx.dry_run else ''}rollback: {text}")
        if not ctx.dry_run:
            fn()

    fresh = rec.get("fresh")
    j = co.job(ctx)
    if fresh and j:
        act(f"bootout new job pid {j['pid']}", lambda: _bootout(ctx))
    if fresh and ctx.data.exists():  # this install made it
        act(f"move {ctx.data} aside", lambda: aside(ctx, ctx.data, "data"))
    if fresh and ctx.plist.exists():
        act("move new plist aside", lambda: aside(ctx, ctx.plist, "plist"))
    claude = rec.get("claude", {}).get("settings")
    if claude and ctx.settings.exists():
        paths = [tuple(e["path"]) for e in claude]
        act(
            f"restore {len(paths)} keys in {ctx.settings.name}",
            lambda: ce.edit_file(
                ctx.settings,
                lambda b: restore_json(b, claude),
                lambda a, b: ce.json_check(a, b, paths),
            ),
        )
    if rec.get("codex"):
        act(
            f"restore {len(rec['codex'])} sections in config.toml",
            lambda: ce.edit_file(
                ctx.config,
                lambda b: restore_toml(b.decode(), rec["codex"]).encode(),
                co.codex_check,
            ),
        )
    for link, target in rec.get("links", {}).items():
        link = Path(link)
        if target is None and link.is_symlink():
            act(f"remove {link}", link.unlink)
        elif target is not None and (
            not link.is_symlink() or os.readlink(link) != target
        ):
            act(
                f"relink {link}",
                lambda lk=link, t=target: co.relink(lk, t, ctx.ts),
            )
    if rec.get("upgrade") and co.job(ctx):  # back onto the old release
        kick = ["launchctl", "kickstart", "-k", ctx.target]
        act("restart the job", lambda: co.must(ctx, kick))
    return problems


def _bootout(ctx):
    ctx.run(["launchctl", "bootout", ctx.target])
    co.wait(ctx, lambda: co.job(ctx) is None, 30, "new job still loaded")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="install.rollback")
    ap.add_argument("--record", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    rec = co.load_record(args.record)
    if Path(rec["home"]).resolve() != Path.home().resolve():
        ap.error("the record belongs to another HOME")
    os.umask(0o077)
    ctx = co.Ctx(
        Path(rec["home"]), co.run_real, rec["ts"], dry_run=args.dry_run
    )
    problems = rollback(ctx, rec)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
