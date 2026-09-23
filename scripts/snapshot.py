"""Build private, source-aware SQLite replacement snapshots."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Optional


def _create_schema(connection: sqlite3.Connection) -> None:
    """Create the disposable snapshot schema."""
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(events)").fetchall()
    }
    if columns and "provider" not in columns:
        connection.executescript(
            "DROP TABLE IF EXISTS assertions; DROP TABLE IF EXISTS sources;"
            "DROP TABLE IF EXISTS event_fts; DROP TABLE IF EXISTS events;"
        )
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, provider TEXT NOT NULL,
            source_path TEXT NOT NULL, source_line INTEGER NOT NULL,
            source_ordinal INTEGER NOT NULL, source_hash TEXT NOT NULL,
            timestamp TEXT, cwd TEXT, repo TEXT, role TEXT NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(provider, source_path, source_line, source_hash,
                   source_ordinal)
        );
        CREATE TABLE IF NOT EXISTS sources (
            provider TEXT NOT NULL, source_path TEXT NOT NULL,
            fingerprint TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 0,
            error TEXT, last_seen REAL NOT NULL,
            PRIMARY KEY(provider, source_path)
        );
        CREATE TABLE IF NOT EXISTS assertions (
            id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES events(id),
            kind TEXT NOT NULL, value TEXT NOT NULL, state TEXT NOT NULL,
            supersedes INTEGER REFERENCES assertions(id),
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_cwd ON events(cwd);
        CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(text);
        """
    )


def open_snapshot(destination: Path) -> tuple[Path, sqlite3.Connection]:
    """Create a private replacement database, copying the prior snapshot."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    if destination.exists():
        shutil.copy2(destination, temporary_path)
    connection = sqlite3.connect(temporary_path)
    _create_schema(connection)
    return temporary_path, connection


def source_rows(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, int, str | None]]:
    """Return known source fingerprints and retry flags."""
    return {
        (provider, path): (fingerprint, pending, error)
        for provider, path, fingerprint, pending, error in connection.execute(
            "SELECT provider, source_path, fingerprint, pending, error "
            "FROM sources"
        )
    }


def _insert_event(
    connection: sqlite3.Connection, event: Mapping[str, object]
) -> int:
    """Insert one event and its lexical index row."""
    cursor = connection.execute(
        """INSERT INTO events(provider, source_path, source_line,
           source_ordinal, source_hash, timestamp, cwd, repo, role, text)
           VALUES(:provider, :source_path, :source_line, :source_ordinal,
                  :source_hash, :timestamp, :cwd, :repo, :role, :text)""",
        event,
    )
    row_id = cursor.lastrowid
    if row_id is None:
        raise sqlite3.Error("event insert did not return a row id")
    connection.execute(
        "INSERT INTO event_fts(rowid, text) VALUES(?, ?)",
        (row_id, event["text"]),
    )
    return int(row_id)


def _preference(text: str) -> Optional[str]:
    """Return a direct first-person preference, never an inferred fact."""
    lowered = text.strip().lower()
    for prefix in ("i prefer ", "please always ", "please never "):
        if lowered.startswith(prefix) and len(text) > len(prefix):
            return text.strip()
    return None


def replace_source(
    connection: sqlite3.Connection,
    provider: str,
    source_path: str,
    fingerprint: str,
    events: Sequence[Mapping[str, object]],
    pending: bool,
) -> int:
    """Replace one parsed source and its direct preference assertions."""
    row_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM events WHERE provider = ? AND source_path = ?",
            (provider, source_path),
        )
    ]
    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        connection.execute(
            f"DELETE FROM assertions WHERE event_id IN ({placeholders})",
            row_ids,
        )
        connection.execute(
            f"DELETE FROM event_fts WHERE rowid IN ({placeholders})", row_ids
        )
    connection.execute(
        "DELETE FROM events WHERE provider = ? AND source_path = ?",
        (provider, source_path),
    )
    count = 0
    for event in events:
        event_id = _insert_event(connection, event)
        if event["role"] == "user" and (
            value := _preference(str(event["text"]))
        ):
            previous = connection.execute(
                """SELECT id FROM assertions WHERE kind = 'preference'
                   AND state = 'active' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if previous:
                connection.execute(
                    "UPDATE assertions SET state = 'superseded' WHERE id = ?",
                    (previous[0],),
                )
            connection.execute(
                """INSERT INTO assertions(event_id, kind, value, state,
                   supersedes, created_at)
                   VALUES(?, 'preference', ?, 'active', ?, ?)""",
                (
                    event_id,
                    value,
                    previous[0] if previous else None,
                    time.time(),
                ),
            )
        count += 1
    connection.execute(
        """INSERT INTO sources(provider, source_path, fingerprint, pending,
           error, last_seen) VALUES(?, ?, ?, ?, NULL, ?)
           ON CONFLICT(provider, source_path) DO UPDATE SET
             fingerprint=excluded.fingerprint, pending=excluded.pending,
             error=NULL, last_seen=excluded.last_seen""",
        (provider, source_path, fingerprint, int(pending), time.time()),
    )
    return count


def mark_source_issue(
    connection: sqlite3.Connection,
    provider: str,
    source_path: str,
    fingerprint: str,
    error: str,
) -> bool:
    """Quarantine a malformed source so stale evidence cannot be recalled."""
    previous = source_rows(connection).get((provider, source_path))
    if previous == (fingerprint, 1, error):
        return False
    row_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM events WHERE provider = ? AND source_path = ?",
            (provider, source_path),
        )
    ]
    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        connection.execute(
            f"DELETE FROM assertions WHERE event_id IN ({placeholders})",
            row_ids,
        )
        connection.execute(
            f"DELETE FROM event_fts WHERE rowid IN ({placeholders})", row_ids
        )
    connection.execute(
        "DELETE FROM events WHERE provider = ? AND source_path = ?",
        (provider, source_path),
    )
    connection.execute(
        """INSERT INTO sources(provider, source_path, fingerprint, pending,
           error, last_seen) VALUES(?, ?, ?, 1, ?, ?)
           ON CONFLICT(provider, source_path) DO UPDATE SET
             fingerprint=excluded.fingerprint, pending=1,
             error=excluded.error, last_seen=excluded.last_seen""",
        (provider, source_path, fingerprint, error, time.time()),
    )
    return True


def delete_missing(
    connection: sqlite3.Connection, current: set[tuple[str, str]]
) -> int:
    """Remove sources that disappeared or were renamed."""
    removed = 0
    for provider, source_path in source_rows(connection):
        if (provider, source_path) in current:
            continue
        row_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM events WHERE provider = ? AND source_path = ?",
                (provider, source_path),
            )
        ]
        if row_ids:
            placeholders = ",".join("?" for _ in row_ids)
            connection.execute(
                f"DELETE FROM assertions WHERE event_id IN ({placeholders})",
                row_ids,
            )
            connection.execute(
                f"DELETE FROM event_fts WHERE rowid IN ({placeholders})",
                row_ids,
            )
        connection.execute(
            "DELETE FROM events WHERE provider = ? AND source_path = ?",
            (provider, source_path),
        )
        connection.execute(
            "DELETE FROM sources WHERE provider = ? AND source_path = ?",
            (provider, source_path),
        )
        removed += 1
    return removed
