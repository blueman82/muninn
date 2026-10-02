"""Poller heartbeat: ``status.json`` and what it says about freshness.

The poller writes counts and timestamps only. Readers (the CLI, the hooks
and doctor) treat a missing or unreadable file as "no heartbeat" rather
than as an error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from pctx import query, store


def read_status(home: Path) -> dict[str, object]:
    """Read ``status.json``.

    Args:
        home: Data directory.

    Returns:
        The parsed object, or ``{}`` when absent, unreadable or not an
        object.
    """
    try:
        data = json.loads((home / "status.json").read_text())
    except (OSError, ValueError):
        return {}
    # json.loads gives Any; JSON object keys are always strings.
    return cast("dict[str, object]", data) if isinstance(data, dict) else {}


def write_status(home: Path, fields: Mapping[str, object]) -> None:
    """Merge fields into ``status.json`` atomically, mode 0600.

    Args:
        home: Data directory.
        fields: Values that replace the same keys in the current file.
    """
    store.write_json_atomic(
        home / "status.json", {**read_status(home), **fields}
    )


def freshness(status: Mapping[str, object]) -> dict[str, object]:
    """Report how old the index is and whether the poller is alive.

    Args:
        status: Parsed ``status.json``; an empty one counts as stale.

    Returns:
        ``index_age_s`` and ``poller`` (``ok`` or ``stale``).
    """
    return query.freshness(status or {})


def install_sha(env: Mapping[str, str]) -> str | None:
    """Name the installed code version.

    Args:
        env: Environment; ``PCTX_INSTALL_SHA`` wins when set.

    Returns:
        The variable, else the name of the pinned code directory that
        ``~/.local/lib/provenance-context/current`` points at, else None.
    """
    if env.get("PCTX_INSTALL_SHA"):
        return env["PCTX_INSTALL_SHA"]
    home = Path(env.get("HOME") or Path.home())
    current = home / ".local/lib/provenance-context/current"
    return current.readlink().name if current.is_symlink() else None
