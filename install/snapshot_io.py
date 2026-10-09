"""Hold private SQLite source identities during backup reads."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from muninn import platform_io, platform_windows


def _private(
    path: Path, metadata: os.stat_result, *, directory: bool = False
) -> None:
    """Refuse foreign, linked or public POSIX state without repairing it."""
    if sys.platform == "win32":
        platform_windows.assert_private(path, directory=directory)
        return
    ordinary = (
        stat.S_ISDIR(metadata.st_mode)
        if directory
        else stat.S_ISREG(metadata.st_mode)
    )
    mode = 0o700 if directory else 0o600
    if (
        not ordinary
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise PermissionError(
            "SQLite state is not ordinary private owner state"
        )


@contextmanager
def validated(path: Path) -> Generator[None]:
    """Keep the source and every existing sidecar private and identity-bound.

    Args:
        path: SQLite file in its private data directory.

    Yields:
        Control while all validation descriptors remain open.

    Raises:
        OSError: If privacy, ordinary type or pathname identity changes.
    """
    _private(path.parent, path.parent.lstat(), directory=True)
    candidates = [path]
    candidates.extend(
        Path(f"{path}{suffix}")
        for suffix in ("-journal", "-wal", "-shm")
        if os.path.lexists(f"{path}{suffix}")
    )
    identities: list[tuple[Path, os.stat_result]] = []
    with ExitStack() as stack:
        for candidate in candidates:
            expected = candidate.lstat()
            _private(candidate, expected)
            handle = stack.enter_context(
                platform_io.open_regular(
                    candidate, root=path.parent, expected=expected
                )
            )
            platform_io.assert_private_fd(handle.fileno())
            metadata = os.fstat(handle.fileno())
            _private(candidate, metadata)
            identities.append((candidate, metadata))
        _unchanged(identities)
        try:
            yield
        finally:
            _unchanged(identities)


def _unchanged(identities: list[tuple[Path, os.stat_result]]) -> None:
    """Require held descriptors to still name their original private paths."""
    for path, original in identities:
        current = path.lstat()
        _private(path, current)
        if (current.st_dev, current.st_ino) != (
            original.st_dev,
            original.st_ino,
        ):
            raise OSError("SQLite source changed identity")
