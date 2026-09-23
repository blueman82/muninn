"""Run failpoint-aware evidence mutations inside caller-owned transactions."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Mapping

CRASH_EXIT_STATUS = 86


def failpoint(name: str) -> None:
    """Raise a named test-only failure at one transaction boundary."""
    if os.environ.get("PROVENANCE_CONTEXT_TEST_FAILPOINT") == name:
        raise RuntimeError(f"deterministic_failpoint_{name}")
    if (
        os.environ.get("PROVENANCE_CONTEXT_TEST_CRASH_ARMED") == "1"
        and os.environ.get("PROVENANCE_CONTEXT_CRASH_FAILPOINT") == name
    ):
        os._exit(CRASH_EXIT_STATUS)


def delete_events(
    connection: sqlite3.Connection, provider: str, source_path: str
) -> None:
    """Delete one source's events and every dependent derived row."""
    row_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM events WHERE provider = ? AND source_path = ?",
            (provider, source_path),
        )
    ]
    if row_ids:
        marks = ",".join("?" for _ in row_ids)
        connection.execute(
            f"DELETE FROM assertions WHERE event_id IN ({marks})", row_ids
        )
        failpoint("after_delete_assertions")
        connection.execute(
            f"DELETE FROM event_fts WHERE rowid IN ({marks})", row_ids
        )
        failpoint("after_delete_fts")
    connection.execute(
        "DELETE FROM events WHERE provider = ? AND source_path = ?",
        (provider, source_path),
    )
    failpoint("after_delete_events")


def insert_event(
    connection: sqlite3.Connection, event: Mapping[str, object]
) -> None:
    """Insert one event with FTS and preference assertions."""
    cursor = connection.execute(
        """INSERT INTO events(provider, source_path, source_line,
           source_ordinal, source_hash, timestamp, cwd, repo, role, text)
           VALUES(:provider, :source_path, :source_line, :source_ordinal,
           :source_hash, :timestamp, :cwd, :repo, :role, :text)""",
        event,
    )
    if cursor.lastrowid is None:
        raise sqlite3.Error("event insert did not return a row id")
    failpoint("after_event_insert")
    connection.execute(
        "INSERT INTO event_fts(rowid, text) VALUES(?, ?)",
        (cursor.lastrowid, event["text"]),
    )
    failpoint("after_fts_insert")
    if event["role"] == "user" and (value := _preference(str(event["text"]))):
        _record_preference(connection, cursor.lastrowid, value)


def _preference(text: str) -> str | None:
    lowered = text.strip().lower()
    for prefix in ("i prefer ", "please always ", "please never "):
        if lowered.startswith(prefix) and len(text) > len(prefix):
            return text.strip()
    return None


def _record_preference(
    connection: sqlite3.Connection, event_id: int, value: str
) -> None:
    previous = connection.execute(
        "SELECT id FROM assertions WHERE kind = 'preference' "
        "AND state = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if previous:
        connection.execute(
            "UPDATE assertions SET state = 'superseded' WHERE id = ?",
            (previous[0],),
        )
        failpoint("after_assertion_supersede")
    connection.execute(
        """INSERT INTO assertions(event_id, kind, value, state, supersedes,
           created_at) VALUES(?, 'preference', ?, 'active', ?, ?)""",
        (event_id, value, previous[0] if previous else None, time.time()),
    )
    failpoint("after_assertion_insert")
