"""Restore the legacy daemon from a cutover record (§6.3 rollback).

  cd ~/.local/lib/provenance-context/current   # or the new repo root
  L=~/.local/share/provenance-context-legacy-<ts>
  python3.13 -E -s -B -m install.rollback \\
      --record "$L/cutover-record.json" [--dry-run]
  ... -m install.rollback --residue   # list leftovers; deletes nothing
Every action first looks at launchd and the filesystem, so rollback is
safe after a failure at any cutover step, a crash, or a second run.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from install import configedit as ce
from install import cutover as co


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
    """Undo whatever the cutover did; returns a list of problems."""
    legacy, problems = Path(rec["legacy"]), []

    def act(text, fn):
        ctx.say(f"{'DRY-RUN ' if ctx.dry_run else ''}rollback: {text}")
        if not ctx.dry_run:
            fn()

    j = co.job(ctx)
    if j and f"{ctx.old_tree}/" not in j["cmd"]:
        act(f"bootout new job pid {j['pid']}", lambda: _bootout(ctx))
    if (legacy / "data").exists():
        if ctx.data.exists():
            act(f"move {ctx.data} aside", lambda: aside(ctx, ctx.data, "data"))
        act(
            "restore legacy data dir",
            lambda: os.rename(legacy / "data", ctx.data),
        )
    elif rec.get("fresh") and ctx.data.exists():  # this install made it
        act(f"move {ctx.data} aside", lambda: aside(ctx, ctx.data, "data"))
    elif ctx.new_data.exists():
        act(
            f"move {ctx.new_data} aside",
            lambda: aside(ctx, ctx.new_data, "new"),
        )
    old_plist = legacy / "com.provenance-context.plist"
    current = ctx.plist.read_bytes() if ctx.plist.exists() else None
    if rec.get("old_plist") and _sha(current) != rec["old_plist"]:
        if _sha(old_plist.read_bytes()) != rec["old_plist"]:
            problems.append(
                "legacy plist copy does not match the recorded hash"
            )
        else:
            data = old_plist.read_bytes()
            act(
                "restore old plist",
                lambda: ce.atomic_write(ctx.plist, data, 0o644),
            )
    elif not rec.get("old_plist") and current is not None:
        act("move new plist aside", lambda: aside(ctx, ctx.plist, "plist"))
    if rec.get("launchd") and co.job(ctx) is None and ctx.plist.exists():
        act("bootstrap old job", lambda: _bootstrap(ctx))
    claude = rec.get("claude", {})
    for path, entries in ((ctx.settings, "settings"), (ctx.known, "known")):
        if claude.get(entries) and path.exists():
            paths = [tuple(e["path"]) for e in claude[entries]]
            act(
                f"restore {len(paths)} keys in {path.name}",
                lambda p=path, e=claude[entries], ps=paths: ce.edit_file(
                    p,
                    lambda b: restore_json(b, e),
                    lambda a, b: ce.json_check(a, b, ps),
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
    stale = legacy / "codex-plugin-cache" / ctx.cache.name
    if stale.exists():
        if ctx.cache.exists():
            act(
                "move new Codex plugin cache aside",
                lambda: aside(ctx, ctx.cache, "codex-cache"),
            )
        act(
            "restore old Codex plugin cache",
            lambda: os.rename(stale, ctx.cache),
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
    legacy_free = rec.get("fresh") or rec.get("upgrade")
    if not legacy_free and co.tree_hash(ctx) != rec["old_tree_hash"]:
        problems.append("OLD TREE CHANGED: hard stop, E1 fails")
    for item in residue(ctx.home):
        ctx.say(f"residue (owner decides): {item['path']} {item['bytes']} B")
    return problems


def _sha(data):
    return data and hashlib.sha256(data).hexdigest()


def _bootout(ctx):
    ctx.run(["launchctl", "bootout", ctx.target])
    co.wait(ctx, lambda: co.job(ctx) is None, 30, "new job still loaded")


def _bootstrap(ctx):
    co.must(ctx, ["launchctl", "bootstrap", f"gui/{ctx.uid}", ctx.plist])
    co.wait(
        ctx,
        lambda: (co.job(ctx) or {}).get("pid"),
        30,
        "old job did not start",
    )


def _size(path):
    if not path.is_dir():
        return path.lstat().st_size
    return sum(
        os.lstat(os.path.join(d, f)).st_size
        for d, _, files in os.walk(path)
        for f in files
    )


def residue(home):
    """Legacy leftovers for the owner (A13/O13); read-only."""
    share = home / ".local/share"
    roots = sorted(share.glob("provenance-context-legacy-*"))
    roots += sorted(share.glob("provenance-context-failed-*"))
    found = [
        r / s
        for r in roots
        for s in ("data", "snapshot", "codex-plugin-cache")
    ]
    found += [home / ".claude/plugins/cache" / co.MKT_NAME]
    found += sorted(
        (home / ".codex/plugins/cache" / co.MKT_NAME).glob("plugin-backup-*")
    )
    paths = roots + [p for p in found if p.exists()]
    return [{"path": str(p), "bytes": _size(p)} for p in paths]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="install.rollback")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--record", type=Path)
    what.add_argument("--residue", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.residue:
        for item in residue(Path.home()):
            print(json.dumps(item))
        return 0
    rec = co.load_record(args.record)
    if Path(rec["home"]).resolve() != Path.home().resolve():
        ap.error("the record belongs to another HOME")
    os.umask(0o077)
    ctx = co.Ctx(
        Path(rec["home"]),
        co.run_real,
        rec["ts"],
        old_tree=Path(rec["old_tree"]),
        dry_run=args.dry_run,
    )
    problems = rollback(ctx, rec)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
