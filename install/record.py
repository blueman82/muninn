"""The rollback record and the install record.

The rollback record holds the before-values of everything an install may
change, so ``install.rollback`` can undo it. The install record is a
shorter, value-free summary left behind for support.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from install import configedit as ce
from install.constants import CODEX_KEYS
from install.context import Ctx
from install.transforms import codex_scan

# A run's state, filled in step by step and saved after every step.
Record = dict[str, Any]

# Fields copied from the run record into the install record.
_SUMMARY_FIELDS = (
    "fresh",
    "upgrade",
    "ts",
    "sha",
    "repo",
    "python",
    "steps",
    "failed",
)


def json_entry(obj: dict[str, Any], path: ce.JsonPath) -> dict[str, Any]:
    """Capture the before-value of one JSON key for rollback.

    Args:
        obj: The parsed settings file.
        path: The key path we may change.

    Returns:
        Whether the key and its parent existed, and if so its value and
        position, so a restore can put it back exactly.
    """
    present, value, index = ce.jget(obj, path)
    entry: dict[str, Any] = {"path": list(path), "present": present}
    entry["parent_present"] = ce.jget(obj, path[:-1])[0] if path[:-1] else True
    if present:
        entry.update(value=value, index=index)
    return entry


def codex_record(text: str) -> list[dict[str, Any]]:
    """Capture the raw before-blocks of our config.toml sections.

    The index is a table position, so no other section's name is recorded;
    config.toml may hold names the owner considers private.

    Args:
        text: The whole config.toml.

    Returns:
        One entry per section of ours, in file order, absent ones last.
    """
    codex_scan(text)
    out = [
        {
            "header": h,
            "raw": ce.get_section(text, h),
            "at": ce.index_of(text, h),
        }
        for h in CODEX_KEYS
    ]
    return sorted(out, key=lambda e: len(text) if e["at"] is None else e["at"])


def save(ctx: Ctx, rec: Record) -> None:
    """Write the rollback record (0600: it holds config before-values)."""
    data = json.dumps(rec, indent=1).encode()
    ce.atomic_write(ctx.rdir / "rollback-record.json", data, 0o600)


def load_record(path: Path | str) -> Record:
    """Read a rollback record written by ``save``."""
    return json.loads(Path(path).read_text())


def install_record(ctx: Ctx, rec: Record, outcome: str) -> None:
    """Write ``lib/install-record.json`` for rollback and support.

    It says what was installed and which config keys were touched: names
    only, never values.

    Args:
        ctx: The run context.
        rec: The run record.
        outcome: ``ok`` or ``rolled_back``.
    """
    if ctx.dry_run:
        return
    ctx.lib.mkdir(parents=True, exist_ok=True)
    keys = [
        ".".join(e["path"])
        for e in rec["claude"]["settings"]
        if rec["has_claude"]
    ]
    keys += [e["header"] for e in rec["codex"] if rec["has_codex"]]
    out = {k: rec.get(k) for k in _SUMMARY_FIELDS}
    out |= {
        "outcome": outcome,
        "config_keys": keys,
        "record": (
            None
            if rec.get("record_removed")
            else str(ctx.rdir / "rollback-record.json")
        ),
    }
    ce.atomic_write(
        ctx.lib / "install-record.json",
        json.dumps(out, indent=1).encode(),
        0o600,
    )
