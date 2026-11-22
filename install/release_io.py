"""Private durable installation state and native release publication."""

from __future__ import annotations

import os
from pathlib import Path

from muninn import file_sync


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
