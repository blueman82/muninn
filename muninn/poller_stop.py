"""Private generation-bound stop requests for a native Windows poller."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import cast

from muninn import platform_io, store

FILE = "stop.json"
_GENERATION = re.compile(r"[0-9a-f]{32}")
_POLL_S = 0.1


def _valid(pid: object, generation: object) -> bool:
    """Recognize only an ordinary pid and a fresh hexadecimal generation."""
    return (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and 0 < pid <= 0xFFFFFFFF
        and isinstance(generation, str)
        and _GENERATION.fullmatch(generation) is not None
    )


def request(home: Path, pid: int, generation: str) -> None:
    """Write an atomic private stop request for exactly one poller generation.

    Args:
        home: Existing private data directory.
        pid: Process id established by lifecycle verification.
        generation: Generation read from that process's heartbeat.

    Raises:
        ValueError: If the target fields are invalid.
        PermissionError: If the data directory is unsafe.
    """
    if not _valid(pid, generation):
        raise ValueError("invalid stop target")
    if not platform_io.is_private(home, directory=True):
        raise PermissionError("stop directory is not private")
    store.write_json_atomic(
        home / FILE, {"pid": pid, "generation": generation}
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


def wait(home: Path, pid: int, generation: str, seconds: float) -> bool:
    """Wait up to the idle interval while checking for stop requests promptly.

    Args:
        home: Private data directory.
        pid: This poller's process id.
        generation: This poller's newly generated token.
        seconds: Maximum idle wait.

    Returns:
        True if a matching request arrived, False when the interval ended.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if requested(home, pid, generation):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_POLL_S, remaining))
