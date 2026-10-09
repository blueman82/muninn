"""Read and classify conversations from Cursor's local SQLite database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from muninn import classify
from muninn.event_model import Draft, Origin, make_event


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
    max_bubbles: int | None,
) -> tuple[CursorEvent, ...] | None:
    """Read visible bubbles, returning None when a bounded read is exceeded."""
    prefix = f"bubbleId:{thread_id}:"
    query = (
        "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?"
        " ORDER BY rowid"
    )
    params: tuple[object, ...] = (prefix, f"{prefix[:-1]};")
    if max_bubbles is not None:
        query += " LIMIT ?"
        params += (max_bubbles + 1,)
    bubbles: dict[str, tuple[dict[str, Any], bytes]] = {}
    for count, (key, raw) in enumerate(conn.execute(query, params), start=1):
        if max_bubbles is not None and count > max_bubbles:
            return None
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


def _decode_composer(
    key: object, raw: object, conversation_id: str | None
) -> tuple[str, dict[str, Any], bytes] | None:
    """Validate a composer cell and optional exact hook identity."""
    thread_id = key[13:] if isinstance(key, str) else ""
    composer, cell = _object(raw), _cell_bytes(raw)
    if not thread_id or composer is None or cell is None:
        if conversation_id is not None:
            raise ValueError("conversation_identity_unavailable")
        return None
    if conversation_id is not None and (
        "composerId" in composer and composer["composerId"] != conversation_id
    ):
        raise ValueError("conversation_identity_mismatch")
    return thread_id, composer, cell


def _composer_rows(
    conn: sqlite3.Connection, conversation_id: str | None
) -> list[tuple[Any, Any]] | sqlite3.Cursor:
    """Select all composers for ingest or an exact, unique hook identity."""
    if conversation_id is not None:
        rows = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key = ? LIMIT 2",
            (f"composerData:{conversation_id}",),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("conversation_identity_unavailable")
        return rows
    return conn.execute(
        "SELECT key, value FROM cursorDiskKV"
        " WHERE substr(key, 1, 13) = 'composerData:' ORDER BY rowid"
    )


def read(
    path: Path,
    max_bubbles: int | None = None,
    *,
    conversation_id: str | None = None,
) -> tuple[list[Conversation], int, tuple[int, int, int]]:
    """Read supported conversations from ``path`` using a read-only handle.

    Args:
        path: Cursor's SQLite database.
        max_bubbles: Cap a hook refresh at Cursor's reported message count.
            None leaves ordinary full imports unbounded.
        conversation_id: Exact hook composer identity; None imports all.

    Returns:
        Conversations, skipped row count, and database file identity.

    Raises:
        ValueError: If the database is unsupported or cannot be read.
    """
    if max_bubbles is not None and not conversation_id:
        raise ValueError("conversation_identity_unavailable")
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
            for key, raw in _composer_rows(conn, conversation_id):
                decoded = _decode_composer(key, raw, conversation_id)
                if decoded is None:
                    skipped[0] += 1
                    continue
                thread_id, composer, cell = decoded
                events = _events(
                    conn, thread_id, composer, skipped, max_bubbles
                )
                if events is None:
                    skipped[0] += 1
                    continue
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
