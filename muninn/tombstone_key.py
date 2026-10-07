"""The tombstone key: the secret behind the content tags in the log.

The key is a random 32-byte file next to the database.  A content tag
(``ev2:`` plus an HMAC-SHA256 of role and text) cannot be guessed from
``tombstones.jsonl`` alone, so an erased short text cannot be confirmed by
dictionary attack.  A new key would silently stop every existing tag from
matching, so one is never made while keyed tombstones exist.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from muninn import platform_io, platform_windows, store
from muninn.file_sync import sync_fd, sync_path

TOMBSTONE_FILE = "tombstones.jsonl"
# A line tombstone whose hash starts with this (HMAC-SHA256 of role and
# text) blocks one event's content rather than one byte-exact line.  Its
# line number is 0, which no transcript line has (they start at 1), so a
# content tag never collides with a line tombstone.
KEYED_PREFIX = "ev2:"
KEY_FILE = "tombstone.key"
KEY_BYTES = 32
# An in-memory database has no data directory to hold a key file; this
# key lives as long as the process, which is as long as that database.
_MEMORY_KEY = os.urandom(KEY_BYTES)


class TombstoneKeyError(Exception):
    """The tombstone key is missing, damaged or unsafe to recreate."""


def _checked(data: bytes) -> bytes:
    """Return a key read from disk, or raise if it is not a whole key."""
    # A short or empty file would still "work" as an HMAC key, but a
    # truncated key is not the key the existing tags were made with.
    if len(data) != KEY_BYTES:
        raise TombstoneKeyError("tombstone key is damaged")
    return data


def _create_key(path: Path) -> bytes:
    """Create the key file whole and return the key it holds.

    The key is written to a private temp file and hard-linked into place,
    so no reader sees a partial file and a creator that loses the race
    reads the winner's complete key.

    Args:
        path: Where the key file belongs.

    Returns:
        The 32-byte key, ours or the winner's.

    Raises:
        TombstoneKeyError: If the winner's file is not a whole key.
    """
    if sys.platform == "win32":
        platform_io.ensure_private_dir(path.parent)
    key = os.urandom(KEY_BYTES)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tombstone.key-")
    try:
        with os.fdopen(fd, "wb") as handle:
            platform_io.assert_private_fd(handle.fileno())
            handle.write(key)
            handle.flush()
            sync_fd(handle.fileno())
        try:
            if sys.platform == "win32":
                platform_windows.publish(Path(temp), path, replace=False)
            else:
                os.link(temp, path)
                sync_path(path.parent)
        except FileExistsError:
            return _read_key(path)
    finally:
        Path(temp).unlink(missing_ok=True)
    return key


def _read_key(path: Path) -> bytes:
    """Read only a whole private, regular key file, never through a link."""
    if sys.platform == "win32":
        platform_windows.assert_private(path)
    with platform_io.open_regular(path) as handle:
        return _checked(handle.read(KEY_BYTES + 1))


def load_key(home: Path, *, create: bool = True) -> bytes:
    """Return the per-install tombstone key, creating it on first need.

    The key makes a content tag unguessable from ``tombstones.jsonl``
    alone, so an erased short text cannot be confirmed by dictionary
    attack.  The file is 0600 and is never logged.  The caller holds the
    writer lock, but a lost creation race is handled anyway.

    Args:
        home: Data directory.
        create: Make a new key when none exists; pass False when keyed
            tombstones already exist, because a new key would silently
            stop them matching.

    Returns:
        The 32-byte key.

    Raises:
        TombstoneKeyError: If the key file is not exactly 32 bytes, or is
            missing and may not be created.
    """
    path = home / KEY_FILE
    try:
        return _read_key(path)
    except FileNotFoundError:
        if not create:
            raise TombstoneKeyError("tombstone key is missing") from None
    return _create_key(path)


def table_has_keyed(conn: sqlite3.Connection) -> bool:
    """Whether the tombstone table holds a keyed content tag."""
    return (
        conn.execute(
            "SELECT 1 FROM tombstone WHERE substr(line_sha256, 1, 4) = ?"
            " LIMIT 1",
            (KEYED_PREFIX,),
        ).fetchone()
        is not None
    )


def log_has_keyed(home: Path) -> bool:
    """Whether ``tombstones.jsonl`` holds a keyed content tag."""
    try:
        data = (home / TOMBSTONE_FILE).read_bytes()
    except OSError:
        return False
    return KEYED_PREFIX.encode() in data


def key_for(conn: sqlite3.Connection) -> bytes:
    """Return the key that belongs to a connection's database.

    The data directory is the database file's directory.  A database with
    no file (in memory) gets a key that lives as long as the process.  A
    key is never made while keyed tombstones exist: those tags could not
    be matched by a new one, so erased text would come back unnoticed.

    Args:
        conn: Any connection to the store.

    Returns:
        The 32-byte key.

    Raises:
        TombstoneKeyError: If the key is damaged, or is missing while
            keyed tombstones exist.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row or not row[2]:
        return _MEMORY_KEY
    home = Path(row[2]).parent
    return load_key(
        home, create=not (table_has_keyed(conn) or log_has_keyed(home))
    )


def _keyed_in_store(home: Path) -> bool:
    """Whether the log or the table (if readable) holds a keyed tag."""
    if log_has_keyed(home):
        return True
    try:
        conn = store.connect_ro(store.db_path(home))
    except store.StoreUnavailableError:
        return False
    try:
        return table_has_keyed(conn)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def key_problem(home: Path) -> str | None:
    """Describe a key that keyed tombstones cannot be matched with.

    Args:
        home: Data directory.

    Returns:
        ``damaged``, or ``missing`` while keyed tombstones exist, else
        None.
    """
    try:
        _read_key(home / KEY_FILE)
    except FileNotFoundError:
        return "missing" if _keyed_in_store(home) else None
    except (TombstoneKeyError, OSError):
        return "damaged"
    return None
