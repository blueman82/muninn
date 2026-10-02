"""Tombstone log: the durable record of what was erased.

A tombstone holds ids and hashes only, never text. The rows live in the
``tombstone`` table and, so a rebuilt index can re-apply them, in a
write-ahead ``tombstones.jsonl`` next to the database.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TypedDict, cast

TOMBSTONE_FILE = "tombstones.jsonl"
_PROVIDERS = ("codex", "claude")
# Columns that identify a tombstone; their order is the INSERT column order.
_KEY = (
    "provider",
    "level",
    "session_root",
    "thread_id",
    "line",
    "line_sha256",
)


class TombstoneRow(TypedDict):
    """One tombstone as stored in the table and in the JSON-lines log."""

    provider: str
    level: str
    session_root: str | None
    thread_id: str | None
    line: int | None
    line_sha256: str | None
    created_at: float


def tombstone_row(
    provider: str,
    level: str,
    *,
    session_root: str | None = None,
    thread_id: str | None = None,
    line: int | None = None,
    line_sha256: str | None = None,
) -> TombstoneRow:
    """Build a tombstone from ids and hashes.

    Only identifiers are accepted, so erased text or the match string can
    never leak into the log by accident.

    Args:
        provider: ``codex`` or ``claude``.
        level: ``session``, ``thread`` or ``line``.
        session_root: Session id, for a session tombstone.
        thread_id: Thread id, for a thread or line tombstone.
        line: 1-based transcript line, for a line tombstone.
        line_sha256: Hash of that line, for a line tombstone.

    Returns:
        The row, stamped with the current time.
    """
    return TombstoneRow(
        provider=provider,
        level=level,
        session_root=session_root,
        thread_id=thread_id,
        line=line,
        line_sha256=line_sha256,
        created_at=time.time(),
    )


def append_tombstones(home: Path, tombstones: list[TombstoneRow]) -> None:
    """Append tombstones to ``tombstones.jsonl`` and force them to disk.

    This runs before the database commit (write-ahead): if the process dies
    between the two, a rebuild can still re-apply the erasure.

    Args:
        home: Data directory.
        tombstones: Rows to append; nothing is written when empty.
    """
    if not tombstones:
        return
    payload = b"".join(
        json.dumps(row, sort_keys=True).encode() + b"\n" for row in tombstones
    )
    # os.open rather than Path.open: the 0600 creation mode and O_APPEND
    # must be atomic with the open so the file is never briefly readable.
    fd = os.open(
        home / TOMBSTONE_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    try:
        # The mode argument only applies on creation; tighten a file that an
        # older version or the user left with looser permissions.
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:  # os.write may be short
            view = view[os.write(fd, view) :]
        try:
            # fsync alone does not flush the drive cache on macOS.
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except (AttributeError, OSError):  # not macOS, or not supported
            os.fsync(fd)
    finally:
        os.close(fd)


def _valid(row: Mapping[str, object]) -> bool:
    """Whether a logged row carries the ids its level needs."""
    level = row.get("level")
    return row.get("provider") in _PROVIDERS and (
        (level == "session" and bool(row.get("session_root")))
        or (level == "thread" and bool(row.get("thread_id")))
        or (
            level == "line"
            and bool(row.get("thread_id"))
            and isinstance(row.get("line"), int)
            and bool(row.get("line_sha256"))
        )
    )


def _logged_rows(path: Path) -> Iterator[dict[str, object]]:
    """Yield the valid rows of the log, skipping damaged lines."""
    # errors="replace": one corrupt byte must not hide every other row.
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            # json.loads gives Any; JSON object keys are always strings.
            typed = cast("dict[str, object]", row)
            if _valid(typed):
                yield typed


def reapply_tombstones(conn: sqlite3.Connection, home: Path) -> int:
    """Insert the logged tombstones that the table lacks.

    This is how erasures survive a rebuilt database. Invalid lines are
    skipped. The caller holds the writer lock.

    Args:
        conn: Read-write connection.
        home: Data directory.

    Returns:
        How many rows were inserted.
    """
    path = home / TOMBSTONE_FILE
    if not path.is_file():
        return 0
    have: set[tuple[object, ...]] = {
        tuple(r)
        for r in conn.execute(f"SELECT {', '.join(_KEY)} FROM tombstone")
    }
    missing: list[tuple[object, ...]] = []
    for row in _logged_rows(path):
        key = tuple(row.get(k) for k in _KEY)
        if key not in have:
            have.add(key)  # the log may repeat a row
            missing.append((row.get("created_at") or time.time(), *key))
    if missing:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            f"INSERT INTO tombstone(created_at, {', '.join(_KEY)})"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            missing,
        )
        conn.execute("COMMIT")
    return len(missing)
