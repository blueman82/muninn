"""Tombstone log: the durable record of what was erased.

A tombstone holds ids and hashes only, never text. The rows live in the
``tombstone`` table and, so a rebuilt index can re-apply them, in a
write-ahead ``tombstones.jsonl`` next to the database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal

from muninn import platform_io
from muninn.file_sync import sync_fd
from muninn.knowledge_erase import (
    erase_attempt_material,
    erase_support_closure,
    plan_replay_support,
    replay_support,
)
from muninn.tombstone_intent import (
    KEY_FIELDS,
    TombstoneRow,
    event_tag,
    read_intent,
    role_digest,
)
from muninn.tombstone_key import KEYED_PREFIX, TOMBSTONE_FILE
from muninn.tombstone_replay import plan_transcripts

__all__ = [
    "TombstoneRow",
    "append_tombstones",
    "erase_attempt_material",
    "erased_ancestor_lines",
    "erased_events",
    "event_digests",
    "event_tag",
    "reapply_tombstones",
    "replay_support",
    "require_current_tombstones",
    "role_digest",
    "tombstone_row",
]


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
        provider: A supported transcript provider.
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
        provider: A supported transcript provider.
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
        provider: A supported transcript provider.
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
    fd = platform_io.open_private(
        home / TOMBSTONE_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND
    )
    try:
        view = memoryview(payload)
        while view:  # os.write may be short
            view = view[os.write(fd, view) :]
        sync_fd(fd)
    finally:
        os.close(fd)


def require_current_tombstones(conn: sqlite3.Connection, home: Path) -> None:
    """Refuse incomplete erasure using only the caller's read snapshot.

    No transaction, key creation, connection, writer or cleanup is owned here.
    Strict intent and remaining transcript targets are checked; successful
    logical completion does not independently certify external file cleanup.

    Args:
        conn: Caller-owned connection with its current transaction view.
        home: Directory containing durable erasure intent.

    Raises:
        ValueError: If intent is malformed, unverified or still pending.
    """
    intent = read_intent(conn, home)
    plan = plan_transcripts(conn, intent)
    if intent.missing or plan.sources or plan.events:
        raise ValueError("erasure_pending")


def reapply_tombstones(conn: sqlite3.Connection, home: Path) -> int:
    """Complete recorded erasure in one transaction, then clean working data.

    The caller holds the physical writer and supplies an autocommit connection.
    Replay never opens or closes that connection or acquires another writer.
    Existing working-file cleanup may authenticate through its bounded
    same-home read-only connection. A postcommit failure propagates and is
    retried even when no keys are missing. Provider originals and retained
    archives remain.

    Args:
        conn: Caller-owned autocommit connection.
        home: Directory containing durable intent and governed working files.

    Returns:
        Number of newly inserted six-field keys after successful cleanup.

    Raises:
        ValueError: If the caller has a transaction or intent cannot be proved.
        OSError: If cleanup fails or a reader prevents WAL truncation.
    """
    if conn.in_transaction:
        raise ValueError("replay_autocommit_required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        intent = read_intent(conn, home)
        target = plan_transcripts(conn, intent)
        support = plan_replay_support(conn, intent.rows)
        conn.executemany(
            f"INSERT INTO tombstone({', '.join(KEY_FIELDS)}, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            intent.missing,
        )
        conn.executemany(
            "DELETE FROM event WHERE id=?", [(i,) for i in target.events]
        )
        erase_support_closure(conn, support, time.time())
        conn.executemany(
            "DELETE FROM source WHERE id=?", [(i,) for i in target.sources]
        )
        conn.execute("COMMIT")
    except BaseException:
        with suppress(BaseException):
            conn.execute("ROLLBACK")
        raise
    for ident in support.requests:
        for row in conn.execute(
            "SELECT id FROM review_attempt WHERE request_id=?", (ident,)
        ):
            erase_attempt_material(home, row[0])
    status = conn.execute("PRAGMA main.wal_checkpoint(TRUNCATE)").fetchone()
    if status[0]:
        raise OSError("erasure_checkpoint_busy")
    require_current_tombstones(conn, home)
    return len(intent.missing)
