"""Seed a fake pre-rename install into a temp HOME for migration tests."""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
from pathlib import Path

from install.constants import (
    OLD_MARKETPLACE,
    OLD_PLUGIN,
    OLD_PLUGIN_ID,
    OLD_TRUST,
)
from tests.installer_support import World, dump

OLD_SHA = "0" * 40
ROWS = {"events": 3, "knowledge": 2}


def _store(path: Path) -> None:
    """Create a small journal-mode-DELETE store with known row counts."""
    conn = sqlite3.connect(path, isolation_level=None)
    for table, n in ROWS.items():
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, t TEXT)")
        for i in range(n):
            conn.execute(f"INSERT INTO {table} (t) VALUES ('{table}{i}')")
    conn.close()


def _provider_config(w: World) -> None:
    """Give Claude and Codex the pre-rename entries plus a foreign one."""
    h = w.home
    old_cmd = f"{h}/.local/bin/pctx hook %s --provider claude"
    group = {"type": "command", "timeout": 5}
    settings = json.loads((h / ".claude/settings.json").read_bytes())
    settings["hooks"] = {
        "SessionStart": [
            {"hooks": [{**group, "command": "/usr/bin/true"}]},
            {"hooks": [{**group, "command": old_cmd % "session-start"}]},
        ],
        "UserPromptSubmit": [
            {"hooks": [{**group, "command": old_cmd % "prompt"}]}
        ],
    }
    (h / ".claude/settings.json").write_bytes(dump(settings))
    toml = h / ".codex/config.toml"
    old = [f'\n{OLD_MARKETPLACE}\nsource_type = "local"\nsource = "/x"\n']
    old.append(f"\n{OLD_PLUGIN}\nenabled = true\n")
    old += [f'\n{t}\ntrusted_hash = "sha256:aa"\n' for t in OLD_TRUST.values()]
    toml.write_text(toml.read_text() + "".join(old))
    cache = h / ".codex/plugins/cache/provenance-context-local"
    (cache / "provenance-context/0.1.5").mkdir(parents=True)
    (cache / "provenance-context/0.1.5/hooks.json").write_text("{}")
    assert OLD_PLUGIN_ID in toml.read_text()


def seed_old(w: World) -> None:
    """Lay out what the live machine had before the rename.

    Args:
        w: A world with plain provider configs; gains the old install and a
            running old launchd job in the fake.
    """
    h = w.home
    lib = h / ".local/lib/provenance-context"
    (lib / OLD_SHA / "bin").mkdir(parents=True)
    (lib / OLD_SHA / "bin/pctx").write_text("#!/bin/sh\n")
    (lib / "current").symlink_to(OLD_SHA)
    (lib / "install-record.json").write_text("{}")
    (h / ".local/bin/pctx").symlink_to(lib / "current/bin/pctx")
    data = h / ".local/share/provenance-context"
    data.mkdir(parents=True, mode=0o700)
    _store(data / "pctx.sqlite")
    for name in ("writer.lock", "status.json", "recall.off", "calls.jsonl"):
        (data / name).write_text(name)
    plist = h / "Library/LaunchAgents/com.provenance-context.plist"
    plist.parent.mkdir(parents=True)
    prog = [f"{lib}/current/bin/pctx", "serve"]
    plist.write_bytes(plistlib.dumps({"ProgramArguments": prog}))
    _provider_config(w)
    w.fake.loaded = "old"


def old_state(h: Path) -> dict[str, bool]:
    """Say which pre-rename artefacts still exist under ``h``."""
    paths = {
        "lib": ".local/lib/provenance-context/current",
        "data": ".local/share/provenance-context/pctx.sqlite",
        "plist": "Library/LaunchAgents/com.provenance-context.plist",
        "cli": ".local/bin/pctx",
        "cache": ".codex/plugins/cache/provenance-context-local",
    }
    return {k: os.path.lexists(h / p) for k, p in paths.items()}


def store_counts(db: Path) -> dict[str, int]:
    """Count the rows of the seeded tables in ``db``."""
    conn = sqlite3.connect(db)
    try:
        return {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ROWS
        }
    finally:
        conn.close()


__all__ = ["ROWS", "old_state", "seed_old", "store_counts"]
