"""Read Cursor's local SQLite database into the Muninn event store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from muninn import classify, scope, store, tombstones
from muninn.event_model import Draft, Origin, make_event
from muninn.tombstone_key import key_for

__all__ = ["default_database", "run"]


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


@dataclass(frozen=True)
class CursorEvent:
    """One supported visible user or assistant bubble."""

    line: int
    line_sha256: str
    role: str
    kind: str
    ts: str | None
    text: str
    flags: int


@dataclass(frozen=True)
class Conversation:
    """A conversation and the content-only identity used for replay."""

    thread_id: str
    first_hash: str
    digest: str
    events: tuple[CursorEvent, ...]


def _object(value: object) -> dict[str, Any] | None:
    """Decode one JSON object cell without trusting its shape."""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (ValueError, RecursionError):
        return None
    return cast(dict[str, Any], decoded) if isinstance(decoded, dict) else None


def _cell_bytes(value: object) -> bytes | None:
    """Return a stable byte representation for a SQLite text cell."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", "surrogatepass")
    return None


def _bubble_ids(composer: dict[str, Any]) -> list[str] | None:
    """Get Cursor's declared bubble order, or None when it is absent."""
    value = composer.get("fullConversationHeadersOnly")
    if not isinstance(value, list):
        return None
    headers = cast(list[Any], value)
    return [
        bubble_id
        for item in headers
        if (bubble_id := _bubble_id(item)) is not None
    ]


def _bubble_id(value: object) -> str | None:
    """Return a bubble id from one optional conversation header."""
    if not isinstance(value, dict):
        return None
    header = cast(dict[str, Any], value)
    bubble_id = header.get("bubbleId")
    return bubble_id if isinstance(bubble_id, str) else None


def _events(
    conn: sqlite3.Connection,
    thread_id: str,
    composer: dict[str, Any],
    skipped: list[int],
) -> tuple[CursorEvent, ...]:
    """Read supported visible text bubbles in Cursor's conversation order."""
    prefix = f"bubbleId:{thread_id}:"
    rows = conn.execute(
        "SELECT key, value FROM cursorDiskKV"
        " WHERE substr(key, 1, ?) = ? ORDER BY rowid",
        (len(prefix), prefix),
    )
    bubbles: dict[str, tuple[dict[str, Any], bytes]] = {}
    for key, raw in rows:
        cell = _cell_bytes(raw)
        if not isinstance(key, str) or cell is None:
            skipped[0] += 1
            continue
        bubble_id = key[len(prefix) :]
        value = _object(raw)
        if not bubble_id or value is None or bubble_id in bubbles:
            skipped[0] += 1
            continue
        bubbles[bubble_id] = (value, cell)
    ordered = _bubble_ids(composer)
    ids = ordered if ordered is not None else list(bubbles)
    events: list[CursorEvent] = []
    for bubble_id in dict.fromkeys(ids):
        item = bubbles.get(bubble_id)
        if item is None:
            skipped[0] += 1
            continue
        bubble, raw = item
        bubble_type, text = bubble.get("type"), bubble.get("text")
        if bubble_type not in (1, 2) or not isinstance(text, str) or not text:
            skipped[0] += 1
            continue
        role = "user" if bubble_type == 1 else "assistant"
        kind = "prompt" if role == "user" else "reply"
        created = bubble.get("createdAt")
        ts = created if isinstance(created, str) else None
        classified = make_event(
            Origin(len(events) + 1, len(events) + 1, ts),
            role,
            Draft(kind, None, text),
        )
        try:
            classified.text.encode("utf-8")
        except UnicodeEncodeError:
            skipped[0] += 1
            continue
        events.append(
            CursorEvent(
                classified.line,
                classify.record_hash(raw),
                classified.role,
                classified.kind,
                ts,
                classified.text,
                classified.flags,
            )
        )
    return tuple(events)


def _read(path: Path) -> tuple[list[Conversation], int, tuple[int, int, int]]:
    """Read supported conversations from ``path`` using a read-only handle."""
    conversations: list[Conversation] = []
    skipped = [0]
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(cursorDiskKV)")
            }
            if not {"key", "value"} <= columns:
                raise ValueError("unsupported_database")
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT key, value FROM cursorDiskKV"
                " WHERE substr(key, 1, 13) = 'composerData:' ORDER BY rowid"
            )
            for key, raw in rows:
                if not isinstance(key, str) or not key[13:]:
                    skipped[0] += 1
                    continue
                composer = _object(raw)
                cell = _cell_bytes(raw)
                if composer is None or cell is None:
                    skipped[0] += 1
                    continue
                thread_id = key[13:]
                events = _events(conn, thread_id, composer, skipped)
                if not events:
                    continue
                digest = hashlib.sha256()
                digest.update(cell)
                for event in events:
                    digest.update(event.line_sha256.encode("ascii"))
                conversations.append(
                    Conversation(
                        thread_id,
                        classify.record_hash(cell),
                        digest.hexdigest(),
                        events,
                    )
                )
            conn.execute("COMMIT")
        finally:
            conn.close()
        stat = path.stat()
        return (
            conversations,
            skipped[0],
            (
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )
    except ValueError:
        raise
    except (OSError, sqlite3.Error, UnicodeError) as exc:
        raise ValueError("unsupported_database") from exc


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
    if row is not None and row["parse_state"] == conversation.digest:
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


def run(home: Path, path: Path, cwd: str, wait_s: float) -> dict[str, int]:
    """Import visible Cursor chat text from one copied SQLite database.

    Args:
        home: Muninn data directory.
        path: User-supplied database file.
        cwd: Scope directory for imported events.
        wait_s: Writer-lock wait limit.

    Returns:
        Counts only; transcript text is never included.

    Raises:
        ValueError: If the file is not a supported Cursor database.
    """
    conversations, skipped, stat = _read(path)
    counts = {
        "conversations_seen": len(conversations),
        "events_added": 0,
        "events_unchanged": 0,
        "skipped_rows": skipped,
        "events_skipped_erased": 0,
    }
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            tombstones.reapply_tombstones(conn, home)
            key = key_for(conn)
            for conversation in conversations:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    added, unchanged, erased = _write_conversation(
                        conn, conversation, str(path.resolve()), stat, cwd, key
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                counts["events_added"] += added
                counts["events_unchanged"] += unchanged
                counts["events_skipped_erased"] += erased
        finally:
            conn.close()
    return counts
