"""Check for a private generation-bound stop request for one poller."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from muninn import platform_io

FILE = "stop.json"


_GENERATION = re.compile(r"[0-9a-f]{32}")


def _valid(pid: object, generation: object) -> bool:
    """Recognize only an ordinary pid and a fresh hexadecimal generation."""
    return (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and 0 < pid <= 0xFFFFFFFF
        and isinstance(generation, str)
        and _GENERATION.fullmatch(generation) is not None
    )


def requested(home: Path, pid: int, generation: str) -> bool:
    """Accept a small private request for this process and generation.

    Args:
        home: Private data directory.
        pid: This poller's process id.
        generation: This poller's newly generated token.

    Returns:
        True for a verified matching request; malformed or unsafe is False.
    """
    path = home / FILE
    if not platform_io.is_private(home, directory=True) or not (
        platform_io.is_private(path)
    ):
        return False
    try:
        with platform_io.open_regular(path, root=home) as handle:
            data = handle.read(1025)
        if len(data) > 1024:
            return False
        value = json.loads(data)
    except (OSError, ValueError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    fields = cast(dict[str, object], value)
    if set(fields) != {"pid", "generation"}:
        return False
    target_pid, target_generation = fields["pid"], fields["generation"]
    return (
        _valid(target_pid, target_generation)
        and target_pid == pid
        and target_generation == generation
    )
