"""Private durable installation state and native release publication."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from muninn import file_sync, platform_io


def publish(
    source: Path,
    target: Path,
    *,
    replace: bool = True,
    directory: bool = False,
) -> None:
    """Publish private state within one volume with no copy/delete fallback.

    Args:
        source: Complete temporary file or directory.
        target: Same-directory final pathname.
        replace: Allow file replacement.
        directory: Require a nonreplacing directory move.

    Raises:
        ValueError: If directory replacement is requested.
        FileExistsError: If a nonreplacing destination exists.
        OSError: If native validation or publication fails.
    """
    if directory and replace:
        raise ValueError("directory publication cannot replace a target")
    if not replace and os.path.lexists(target):
        raise FileExistsError("publication target already exists")
    source.replace(target)
    file_sync.sync_path(target.parent)


def write_private(path: Path, data: bytes) -> None:
    """Flush a validated private temporary file before atomic publication."""
    platform_io.ensure_private_dir(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    # Keep pre-publication failures available for recovery; publish moves it.
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        file_sync.sync_fd(handle.fileno())
    publish(temporary, path)
