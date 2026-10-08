"""Private durable installation state and native release publication."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from muninn import file_sync, platform_io, platform_windows


def publish(source: Path, target: Path, *, replace: bool = True) -> None:
    """Publish within one volume, retaining the source on uncertain failure."""
    if os.name == "nt":
        platform_windows.publish(source, target, replace=replace)
    else:
        source.replace(target)
        file_sync.sync_path(target.parent)


def write_private(path: Path, data: bytes) -> None:
    """Flush a validated private temporary file before atomic publication."""
    platform_io.ensure_private_dir(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            platform_io.assert_private_fd(handle.fileno())
            handle.write(data)
            handle.flush()
            file_sync.sync_fd(handle.fileno())
        publish(temporary, path)
    except BaseException:
        # The unpublished copy remains evidence when publication is uncertain.
        raise


def write_selection(lib: Path, sha: str, python: str) -> None:
    """Commit only the two fields consumed by the validated native launcher."""
    write_private(
        lib / "selection.json",
        json.dumps({"sha": sha, "python": python}).encode(),
    )
