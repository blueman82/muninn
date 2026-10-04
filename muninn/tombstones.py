"""Tombstone log: the durable record of what was erased.

A tombstone holds ids and hashes only, never text. The rows live in the
``tombstone`` table and, so a rebuilt index can re-apply them, in a
write-ahead ``tombstones.jsonl`` next to the database.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Literal, TypedDict, cast

from muninn.tombstone_key import KEYED_PREFIX, TOMBSTONE_FILE

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
    level: Literal["session", "thread", "line"]
    session_root: str | None
    thread_id: str | None
    line: int | None
    line_sha256: str | None
    created_at: float


def tombstone_row(
    provider: str,
    level: Literal["session", "thread", "line"],
    *,
    session_root: str | None = None,
    thread_id: str | None = None,
    line: int | None = None,
    line_sha256: str | None = None,
) -> TombstoneRow:
    """Build a tombstone from ids and hashes.

    The row has a fixed set of fields, so no free-form field can be added.

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


def role_digest(role: str, text: str) -> bytes:
    """Return sha256(role, text), the identity of one event's content.

    A fork's early events are compared with its parent's by this hash in
    memory only; it is never stored (see ``event_tag`` for what is).
    ``surrogatepass`` keeps lone surrogates from a lossy transcript
    hashable instead of raising.

    Args:
        role: Event role.
        text: Event text.

    Returns:
        The 32-byte digest.
    """
    data = f"{role}\n{text}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).digest()


def event_digests(
    conn: sqlite3.Connection, source_id: int | None
) -> set[bytes]:
    """Return the ``role_digest`` of every event of a source (in memory)."""
    if source_id is None:
        return set()
    rows = conn.execute(
        "SELECT role, text FROM event WHERE source_id = ?", (source_id,)
    )
    return {role_digest(role, text) for role, text in rows}


def event_tag(key: bytes, role: str, text: str) -> str:
    """Return the ``line_sha256`` value of a content tombstone."""
    data = f"{role}\n{text}".encode("utf-8", "surrogatepass")
    return KEYED_PREFIX + hmac.new(key, data, hashlib.sha256).hexdigest()


def _family(
    conn: sqlite3.Connection,
    provider: str,
    thread_id: str,
    forked_from_id: str | None,
) -> tuple[set[str], set[str]]:
    """Return a thread's fork family and its ancestors (own id excluded)."""
    ancestors: set[str] = set()
    parent = forked_from_id
    while parent and parent != thread_id and parent not in ancestors:
        ancestors.add(parent)  # a loop ends the walk
        row = conn.execute(
            "SELECT forked_from_id FROM source WHERE provider = ?"
            " AND thread_id = ?",
            (provider, parent),
        ).fetchone()
        parent = row[0] if row else None
    return {thread_id} | ancestors, ancestors


def erased_events(
    conn: sqlite3.Connection,
    provider: str,
    thread_id: str,
    forked_from_id: str | None,
) -> set[str]:
    """Return the content tags erased in a thread or any of its ancestors.

    A fork copies its parent's history under a new thread id, and perhaps
    new line numbers, so a line tombstone cannot find the copy; the tag of
    the event's role and text can.  The scope is the fork family, not the
    whole provider: the same text typed in an unrelated session is not
    residue of the erase.

    Args:
        conn: Any connection to the store.
        provider: ``codex`` or ``claude``.
        thread_id: The thread being read.
        forked_from_id: Its parent thread, if it is a fork.

    Returns:
        The ``ev2:`` tags of the family.
    """
    family, _ = _family(conn, provider, thread_id, forked_from_id)
    marks = ",".join("?" * len(family))
    return {
        r[0]
        for r in conn.execute(
            "SELECT line_sha256 FROM tombstone WHERE provider = ? AND"
            f" level = 'line' AND line = 0 AND thread_id IN ({marks})"
            " AND substr(line_sha256, 1, 4) = ?",
            (provider, *family, KEYED_PREFIX),
        )
    }


def erased_ancestor_lines(
    conn: sqlite3.Connection,
    provider: str,
    thread_id: str,
    forked_from_id: str | None,
) -> set[str]:
    """Return the hashes of lines erased in a thread's ancestors.

    An erase made before content tags existed left only these, and a fork
    that copies the line byte for byte can still be recognised by its
    hash.

    Args:
        conn: Any connection to the store.
        provider: ``codex`` or ``claude``.
        thread_id: The thread being read.
        forked_from_id: Its parent thread, if it is a fork.

    Returns:
        The line hashes of the ancestors' line tombstones.
    """
    _, ancestors = _family(conn, provider, thread_id, forked_from_id)
    if not ancestors:
        return set()
    marks = ",".join("?" * len(ancestors))
    return {
        r[0]
        for r in conn.execute(
            "SELECT line_sha256 FROM tombstone WHERE provider = ? AND"
            f" level = 'line' AND line > 0 AND thread_id IN ({marks})",
            (provider, *ancestors),
        )
    }


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
    return (
        row.get("provider") in _PROVIDERS
        and (
            (level == "session" and bool(row.get("session_root")))
            or (level == "thread" and bool(row.get("thread_id")))
            or (
                level == "line"
                and bool(row.get("thread_id"))
                and _is_int(row.get("line"))
                and bool(row.get("line_sha256"))
            )
        )
        and _is_number(row.get("created_at"))
    )


def _is_int(value: object) -> bool:
    """Whether a value is an int; ``bool`` is a subclass but not a line."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    """Whether a log field is absent or a real timestamp."""
    return value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
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
