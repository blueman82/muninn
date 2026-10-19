"""Compare complete private source files across caller-owned writer gaps."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from muninn import platform_io, platform_windows, store
from muninn.tombstone_key import KEY_FILE, TOMBSTONE_FILE

_CHUNK_BYTES = 1024 * 1024


class SourceChangedError(ValueError):
    """Refuse changed or uninspectable source state without content details."""

    def __init__(self) -> None:
        """Use the same bounded code for every source inspection refusal."""
        super().__init__("source_changed")


@dataclass(frozen=True)
class FileFingerprint:
    """Bind one private file's identity, metadata and complete byte digest."""

    identity: tuple[int, int]
    size: int
    mtime_ns: int
    ctime_ns: int
    digest: str


@dataclass(frozen=True)
class Fingerprint:
    """Keep immutable original source observations for later comparison."""

    home_identity: tuple[int, int]
    database: FileFingerprint
    key: FileFingerprint | None
    journal: FileFingerprint | None


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    """Select the native device and file identity without resolving links."""
    return metadata.st_dev, metadata.st_ino


def _stamp(metadata: os.stat_result) -> tuple[tuple[int, int], int, int, int]:
    """Ignore read access time while detecting changes during inspection."""
    return (
        _identity(metadata),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _same_discovery(opened: os.stat_result, expected: os.stat_result) -> bool:
    """Compare path and descriptor views using a shared creation timestamp."""
    if sys.platform != "win32":
        return _stamp(opened) == _stamp(expected)
    # Windows path ctime is creation time; descriptor ctime is change time.
    return (
        _identity(opened),
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_birthtime_ns,
    ) == (
        _identity(expected),
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_birthtime_ns,
    )


def _private_stat(
    metadata: os.stat_result, *, directory: bool = False
) -> None:
    """Require ordinary type and exact owner-only POSIX access controls."""
    ordinary = stat.S_ISDIR if directory else stat.S_ISREG
    if not ordinary(metadata.st_mode):
        raise SourceChangedError
    if sys.platform != "win32" and (
        metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != (0o700 if directory else 0o600)
    ):
        raise SourceChangedError


def _metadata(path: Path, *, directory: bool = False) -> os.stat_result:
    """Inspect privacy without following a final link or changing its mode."""
    metadata = path.lstat()
    _private_stat(metadata, directory=directory)
    if sys.platform == "win32":
        platform_windows.assert_private(path, directory=directory)
    return metadata


def _matches(path: Path, original: FileFingerprint | None) -> None:
    """Recheck presence and metadata after other files have been inspected."""
    try:
        actual = _metadata(path)
    except FileNotFoundError:
        if original is not None:
            raise SourceChangedError from None
        return
    if original is None or _stamp(actual) != (
        original.identity,
        original.size,
        original.mtime_ns,
        original.ctime_ns,
    ):
        raise SourceChangedError


def _read(path: Path, expected: os.stat_result) -> FileFingerprint:
    """Stream a nofollow descriptor and refuse changes before closing it."""
    handle = platform_io.open_regular(
        path, root=path.parent, expected=expected
    )
    try:
        platform_io.assert_private_fd(handle.fileno())
        opened = os.fstat(handle.fileno())
        _private_stat(opened)
        if not _same_discovery(opened, expected):
            raise SourceChangedError
        digest = hashlib.sha256()
        count = 0
        while block := handle.read(_CHUNK_BYTES):
            digest.update(block)
            count += len(block)
        if count != expected.st_size or (
            _stamp(os.fstat(handle.fileno())) != _stamp(opened)
            or _stamp(_metadata(path)) != _stamp(expected)
        ):
            raise SourceChangedError
        result = FileFingerprint(*_stamp(expected), digest.hexdigest())
    except BaseException:
        # A close error must not replace the first read or identity failure.
        with contextlib.suppress(BaseException):
            handle.close()
        raise
    else:
        handle.close()
        return result


def capture_file(path: Path) -> FileFingerprint:
    """Bind the complete bytes of one existing private ordinary file.

    The caller owns any required maintenance barriers. This opens no
    SQLite connection and does not validate database contents or format.

    Args:
        path: File in an ordinary private parent directory.

    Returns:
        Immutable file identity, change metadata and complete SHA-256 digest.

    Raises:
        SourceChangedError: If identity, privacy or a complete read is
            uncertain.
    """
    try:
        home = _identity(_metadata(path.parent, directory=True))
        result = _read(path, _metadata(path))
        if _identity(_metadata(path.parent, directory=True)) != home:
            raise SourceChangedError
        return result
    except OSError as exc:
        raise SourceChangedError from exc


def _optional(path: Path) -> FileFingerprint | None:
    """Bind absence or the entire private file without a permissive exists."""
    try:
        expected = _metadata(path)
    except FileNotFoundError:
        return None
    return _read(path, expected)


def _no_sidecars(database: Path) -> None:
    """Refuse every sidecar entry, including unreadable and dangling links."""
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            Path(f"{database}{suffix}").lstat()
        except FileNotFoundError:
            continue
        raise SourceChangedError


def capture(home: Path) -> Fingerprint:
    """Bind the settled source and current key and erasure-journal bytes.

    The caller must hold its writer barrier and close SQLite connections
    before this call. These observations detect cooperative intervening
    writes; they are not SQLite descriptor authentication or an ABA proof.
    The caller validates DELETE journal mode and current store semantics.

    Args:
        home: Private data directory containing the required database.

    Returns:
        Immutable source fingerprint to retain across all writer gaps.

    Raises:
        SourceChangedError: If any state is unsafe, unreadable or unsettled.
    """
    try:
        identity = _identity(_metadata(home, directory=True))
        database = store.db_path(home)
        _no_sidecars(database)
        db = _read(database, _metadata(database))
        key = _optional(home / KEY_FILE)
        journal = _optional(home / TOMBSTONE_FILE)
        for path, original in (
            (database, db),
            (home / KEY_FILE, key),
            (home / TOMBSTONE_FILE, journal),
        ):
            _matches(path, original)
        _no_sidecars(database)
        if _identity(_metadata(home, directory=True)) != identity:
            raise SourceChangedError
        return Fingerprint(identity, db, key, journal)
    except OSError as exc:
        raise SourceChangedError from exc


def verify(home: Path, original: Fingerprint) -> None:
    """Refuse a changed source without replacing the caller's original.

    Args:
        home: Current private source home, inspected under the caller's writer.
        original: Frozen observation from before any candidate work or gap.

    Raises:
        SourceChangedError: If any bound state differs or cannot be inspected.
    """
    if capture(home) != original:
        raise SourceChangedError
