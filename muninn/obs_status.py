"""Poller heartbeat: ``status.json`` and what it says about freshness.

The poller writes counts and timestamps only. Readers (the CLI, the hooks
and doctor) treat a missing or unreadable file as "no heartbeat" rather
than as an error.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from muninn import platform_io, query, store
from muninn.platform_paths import read_selection, windows_base


def read_status(home: Path) -> dict[str, object]:
    """Read ``status.json``.

    Args:
        home: Data directory.

    Returns:
        The parsed object, or ``{}`` when absent, unreadable or not an
        object.
    """
    try:
        path = home / "status.json"
        with platform_io.open_regular(path, root=home) as handle:
            platform_io.assert_private_fd(handle.fileno())
            raw = handle.read(16385)
        if len(raw) > 16384:
            return {}
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    # json.loads gives Any; JSON object keys are always strings.
    return cast("dict[str, object]", data) if isinstance(data, dict) else {}


_ERROR_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def poller_error(status: Mapping[str, object]) -> str | None:
    """Return the poller's last error class name, if it is one.

    ``status.json`` is read back from disk and could be hand-edited, so only
    a bare identifier passes; anything else could smuggle text into doctor
    or a hook block.

    Args:
        status: Parsed ``status.json``.

    Returns:
        The exception class name, or None when absent or malformed.
    """
    err = status.get("last_error")
    if isinstance(err, str) and _ERROR_CODE.fullmatch(err):
        return err
    return None


def count_field(status: Mapping[str, object], key: str) -> int:
    """Read a count from ``status.json``, treating anything odd as zero.

    Args:
        status: Parsed ``status.json``.
        key: Field name.

    Returns:
        The non-negative int, else 0.
    """
    n = status.get(key)
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


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
        env: Environment; ``MUNINN_INSTALL_SHA`` wins when set.

    Returns:
        The variable, else the name of the pinned code directory that
        ``~/.local/lib/muninn/current`` points at, else None.
    """
    if env.get("MUNINN_INSTALL_SHA"):
        return env["MUNINN_INSTALL_SHA"]
    if sys.platform == "win32":
        try:
            selected = read_selection(windows_base(env))
        except (OSError, ValueError):
            return None
        return selected[0].name if selected else None
    home = Path(env.get("HOME") or Path.home())
    current = home / ".local/lib/muninn/current"
    return current.readlink().name if current.is_symlink() else None
