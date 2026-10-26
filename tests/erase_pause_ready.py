"""Bound readiness reads and cleanup for disposable owner fixtures."""

from __future__ import annotations

import errno
import subprocess
import time
from pathlib import Path


def read_ready_text(path: Path) -> str:
    """Wait for a complete readable marker within the original eight seconds.

    Args:
        path: Disposable marker published atomically by the fixture owner.

    Returns:
        The fresh marker contents without a separate existence check.

    Raises:
        FileNotFoundError: If the marker remains absent at the deadline.
        PermissionError: If access remains unavailable or fails unexpectedly.
        TimeoutError: If the deadline passes without a previous read error.
    """
    deadline = time.monotonic() + 8
    last_error: FileNotFoundError | PermissionError | None = None
    while True:
        _remaining(deadline, last_error)
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as error:
            if (
                isinstance(error, PermissionError)
                and error.errno != errno.EACCES
            ):
                raise
            last_error = error
            time.sleep(min(0.01, _remaining(deadline, last_error)))
            continue
        _remaining(deadline, last_error)
        return text


def _remaining(
    deadline: float, error: FileNotFoundError | PermissionError | None
) -> float:
    """Refuse expired readiness waits without replacing a native failure.

    Raises:
        FileNotFoundError: If the last marker read reported absence.
        PermissionError: If the last marker read reported denied access.
        TimeoutError: If no earlier read error explains the expired budget.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if error is not None:
            raise error
        raise TimeoutError("fixture readiness exceeded eight seconds")
    return remaining


def finish_owner(
    proc: subprocess.Popen[bytes], release: Path, *, started: bool
) -> None:
    """Join fixture cleanup without expecting a stop after failed startup.

    Args:
        proc: The disposable owner process whose cleanup must finish.
        release: Marker releasing the fixture's final acknowledgement wait.
        started: Whether readiness parsing completed before caller failure.

    Raises:
        AssertionError: If a started fixture exits unsuccessfully.
    """
    release.touch()
    try:
        _, errors = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, errors = proc.communicate(timeout=5)
    if started and proc.returncode != 0:
        raise AssertionError(
            f"fixture exited {proc.returncode}: {errors.decode()}"
        )
