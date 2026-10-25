"""Read Cursor's local SQLite database into the Muninn event store."""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from muninn import classify, scope, store, tombstones
from muninn.cursor_import_read import Conversation, read
from muninn.tombstone_key import key_for

__all__ = ["PreparedImport", "apply", "default_database", "prepare", "run"]


def default_database(
    home: Path, *, env: Mapping[str, str] | None = None
) -> Path | None:
    """Select an explicit native database or the established macOS path.

    Args:
        home: Provider home for the macOS default.
        env: Optional explicit MUNINN_CURSOR_DB input; omitted ignores it.

    Returns:
        The database path, or None off macOS without explicit input.

    Raises:
        ValueError: If the explicit input is not a native absolute path.
    """
    explicit = (env or {}).get("MUNINN_CURSOR_DB")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute() or "\0" in explicit:
            raise ValueError("MUNINN_CURSOR_DB must be a native absolute path")
        return path
    if sys.platform != "darwin":
        return None
    return home / (
        "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    )


def _tombstoned(conn: sqlite3.Connection, thread_id: str) -> bool:
    """Whether erasure forbids importing this Cursor conversation."""
    return (
        conn.execute(
            "SELECT 1 FROM tombstone WHERE provider = 'cursor' AND ("
            "(level = 'thread' AND thread_id = ?) OR "
            "(level = 'session' AND session_root = ?)) LIMIT 1",
            (thread_id, thread_id),
        ).fetchone()
        is not None
    )


def _write_conversation(
    conn: sqlite3.Connection,
    conversation: Conversation,
    database: str,
    stat: tuple[int, int, int],
    cwd: str,
    key: bytes,
    refresh_scope: bool,
) -> tuple[int, int, int]:
    """Store one conversation; return added, unchanged and skipped counts."""
    thread = conversation.thread_id
    if _tombstoned(conn, thread):
        return 0, 0, len(conversation.events)
    row = conn.execute(
        "SELECT id, parse_state FROM source WHERE provider = 'cursor'"
        " AND thread_id = ?",
        (thread,),
    ).fetchone()
    now = time.time()
    if (
        row is not None
        and row["parse_state"] == conversation.digest
        and (not refresh_scope or _same_scope(conn, row["id"], cwd))
    ):
        conn.execute(
            "UPDATE source SET path=?, status='active', last_seen=?"
            " WHERE id=?",
            (f"{database}#{thread}", now, row["id"]),
        )
        stored = conn.execute(
            "SELECT count(*) FROM event WHERE source_id = ?", (row["id"],)
        ).fetchone()[0]
        return 0, stored, len(conversation.events) - stored
    sid = scope.scope_id(conn, cwd)
    source_id = _upsert_source(conn, conversation, database, stat, now, row)
    erased = tombstones.erased_events(conn, "cursor", thread, None)
    added = 0
    skipped = 0
    for event in conversation.events:
        if event.line_sha256 in _line_tombstones(conn, thread, event.line):
            skipped += 1
            continue
        if tombstones.event_tag(key, event.role, event.text) in erased:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO event(source_id, line, part, byte_offset,"
            " line_sha256, seq, ts, role, kind, scope_id, cwd, flags, text)"
            " VALUES (?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                event.line,
                event.line_sha256,
                event.line,
                event.ts,
                event.role,
                event.kind,
                sid,
                cwd,
                event.flags,
                event.text,
            ),
        )
        added += 1
    return added, 0, skipped


def _same_scope(conn: sqlite3.Connection, source_id: int, cwd: str) -> bool:
    """Require authoritative scope for an unchanged hook import."""
    sid = scope.scope_id(conn, cwd)
    return (
        conn.execute(
            "SELECT 1 FROM event WHERE source_id=?"
            " AND (cwd IS NOT ? OR scope_id IS NOT ?) LIMIT 1",
            (source_id, cwd, sid),
        ).fetchone()
        is None
    )


def _upsert_source(
    conn: sqlite3.Connection,
    conversation: Conversation,
    database: str,
    stat: tuple[int, int, int],
    now: float,
    row: sqlite3.Row | None,
) -> int:
    """Insert a source row or replace its stored import identity."""
    thread = conversation.thread_id
    ino, size, mtime_ns = stat
    source_id = row["id"] if row else None
    if source_id is None:
        return conn.execute(
            "INSERT INTO source(provider, thread_id, session_root,"
            " thread_class, class_reason, replay_mode, root, path,"
            " first_line_sha256, ino, size, mtime_ns, classifier_version,"
            " first_seen, last_seen, parse_state)"
            " VALUES ('cursor', ?, ?, 'primary', 'cursor_import', 'none',"
            " 'cursor-imports', ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                thread,
                thread,
                f"{database}#{thread}",
                conversation.first_hash,
                ino,
                size,
                mtime_ns,
                classify.CLASSIFIER_VERSION,
                now,
                now,
                conversation.digest,
            ),
        ).fetchone()[0]
    conn.execute("DELETE FROM event WHERE source_id = ?", (source_id,))
    conn.execute(
        "UPDATE source SET path=?, first_line_sha256=?, ino=?, size=?,"
        " mtime_ns=?, classifier_version=?, parse_state=?, status='active',"
        " last_seen=? WHERE id=?",
        (
            f"{database}#{thread}",
            conversation.first_hash,
            ino,
            size,
            mtime_ns,
            classify.CLASSIFIER_VERSION,
            conversation.digest,
            now,
            source_id,
        ),
    )
    return source_id


def _line_tombstones(
    conn: sqlite3.Connection, thread_id: str, line: int
) -> set[str]:
    """Return raw-content hashes erased at one Cursor bubble position."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT line_sha256 FROM tombstone WHERE provider='cursor'"
            " AND thread_id=? AND line=?",
            (thread_id, line),
        )
    }


@dataclass(frozen=True)
class PreparedImport:
    """Decoded read-only input, not authority or a source-freshness proof."""

    conversations: tuple[Conversation, ...]
    skipped_rows: int
    stat: tuple[int, int, int]
    database: str
    cwd: str
    conversation_id: str | None


def prepare(
    path: Path,
    cwd: str,
    *,
    max_bubbles: int | None = None,
    conversation_id: str | None = None,
) -> PreparedImport:
    """Read Cursor's database before the caller acquires its writer.

    Args:
        path: User-supplied Cursor database file.
        cwd: Scope directory for imported events.
        max_bubbles: Hook message-count bound; None imports without a cap.
        conversation_id: Exact hook composer identity; None imports all.

    Returns:
        Decoded conversations and their read-time metadata. The provider may
        change after the read; this proves neither identity nor pause.

    Raises:
        ValueError: If the file is unsupported or the selection is invalid.
    """
    conversations, skipped, stat = read(
        path, max_bubbles, conversation_id=conversation_id
    )
    return PreparedImport(
        tuple(conversations),
        skipped,
        stat,
        str(path.resolve()),
        cwd,
        conversation_id,
    )


def _counts(prepared: PreparedImport) -> dict[str, int]:
    """Initialize independent counters for one application."""
    return {
        "conversations_seen": len(prepared.conversations),
        "events_added": 0,
        "events_unchanged": 0,
        "skipped_rows": prepared.skipped_rows,
        "events_skipped_erased": 0,
    }


def apply(
    conn: sqlite3.Connection, home: Path, prepared: PreparedImport
) -> dict[str, int]:
    """Import prepared conversations using the caller's writer connection.

    The caller must hold the actual writer, open the current marked store
    inside that region, and close it before release or pathname replacement.
    Path validation cannot authenticate SQLite's backing descriptor. This
    function acquires no lock, opens no writable connection, never closes the
    caller's connection, and starts no process. Delegated tombstone cleanup
    may open the same current store read-only, solely to authenticate existing
    governed attempt directories under the caller's writer. Those auxiliary
    connections close before cleanup returns or raises.

    Replay commits logical scrub before filesystem cleanup. Cleanup failures
    propagate before any Cursor writes; they do not undo committed scrub.
    Earlier conversation commits also survive a later failure.

    Args:
        conn: Caller-owned current store.connect_rw connection without a TX.
        home: Muninn data directory owning the store and tombstone journal.
        prepared: Data returned by prepare, without any ownership authority.

    Returns:
        Counts only, accumulated after each conversation commits.

    Raises:
        ValueError: If the connection uses transactions or another store path.
        store.StoreUnavailableError: If the current format marker is absent.
    """
    autocommit = conn.autocommit is True or (
        conn.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
        and conn.isolation_level is None
    )
    if not autocommit or conn.in_transaction:
        raise ValueError("cursor_import_autocommit_required")
    paths = [
        row[2]
        for row in conn.execute("PRAGMA database_list")
        if row[1] == "main"
    ]
    if (
        not paths
        or not paths[0]
        or Path(paths[0]).resolve() != store.db_path(home).resolve()
    ):
        raise ValueError("cursor_import_store_mismatch")
    store.assert_store_format(conn)
    counts = _counts(prepared)
    if prepared.conversation_id is not None and not prepared.conversations:
        return counts
    tombstones.reapply_tombstones(conn, home)
    key = key_for(conn)
    for conversation in prepared.conversations:
        conn.execute("BEGIN IMMEDIATE")
        try:
            added, unchanged, erased = _write_conversation(
                conn,
                conversation,
                prepared.database,
                prepared.stat,
                prepared.cwd,
                key,
                prepared.conversation_id is not None,
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        counts["events_added"] += added
        counts["events_unchanged"] += unchanged
        counts["events_skipped_erased"] += erased
    return counts


def run(
    home: Path,
    path: Path,
    cwd: str,
    wait_s: float,
    *,
    max_bubbles: int | None = None,
    conversation_id: str | None = None,
) -> dict[str, int]:
    """Import visible Cursor chat text using a read-only SQLite snapshot.

    Args:
        home: Muninn data directory.
        path: User-supplied database file.
        cwd: Scope directory for imported events.
        wait_s: Writer-lock wait limit.
        max_bubbles: Limit a hook refresh using Cursor's reported message
            count.
        conversation_id: Exact hook composer identity; None imports all.

    Returns:
        Counts only; transcript text is never included.

    Raises:
        ValueError: If the file is not a supported Cursor database.
    """
    prepared = prepare(
        path, cwd, max_bubbles=max_bubbles, conversation_id=conversation_id
    )
    if conversation_id is not None and not prepared.conversations:
        return _counts(prepared)
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            return apply(conn, home, prepared)
        finally:
            conn.close()
