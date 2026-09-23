"""Maintain the daemon-owned incremental SQLite WAL evidence store."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = "2"
BUSY_TIMEOUT_MS = 200


def open_store(database: Path) -> sqlite3.Connection:
    """Open the private daemon database with bounded WAL reader behavior."""
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_MS / 1000)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return connection


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
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
            identity TEXT NOT NULL, fingerprint TEXT NOT NULL,
            cursor_bytes INTEGER NOT NULL DEFAULT 0,
            cursor_line INTEGER NOT NULL DEFAULT 0,
            pending INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
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
    connection.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


def needs_migration(database: Path) -> bool:
    """Return whether the derived database is absent or not the WAL schema."""
    if not database.exists():
        return True
    try:
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
    except sqlite3.Error:
        return True
    return row != (SCHEMA_VERSION,)


def initialize(database: Path, rollback: Path) -> bool:
    """Create v2 storage, retaining one v1/corrupt rollback artifact."""
    migrating = needs_migration(database)
    if migrating and database.exists():
        rollback.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(database, rollback)
        database.unlink()
        for suffix in ("-wal", "-shm"):
            database.with_name(f"{database.name}{suffix}").unlink(
                missing_ok=True
            )
    with closing(open_store(database)) as connection:
        with connection:
            _schema(connection)
    os.chmod(database, 0o600)
    return migrating


def source_rows(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, object]]:
    """Return source cursor and health state keyed by provider/path."""
    return {
        (row[0], row[1]): {
            "identity": row[2],
            "fingerprint": row[3],
            "cursor_bytes": row[4],
            "cursor_line": row[5],
            "pending": bool(row[6]),
            "status": row[7],
            "error": row[8],
        }
        for row in connection.execute(
            "SELECT provider, source_path, identity, fingerprint, "
            "cursor_bytes, "
            "cursor_line, pending, status, error FROM sources"
        )
    }


def _delete_events(
    connection: sqlite3.Connection, provider: str, source_path: str
) -> None:
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
        connection.execute(
            f"DELETE FROM event_fts WHERE rowid IN ({marks})", row_ids
        )
    connection.execute(
        "DELETE FROM events WHERE provider = ? AND source_path = ?",
        (provider, source_path),
    )


def _preference(text: str) -> str | None:
    lowered = text.strip().lower()
    for prefix in ("i prefer ", "please always ", "please never "):
        if lowered.startswith(prefix) and len(text) > len(prefix):
            return text.strip()
    return None


def _insert_event(
    connection: sqlite3.Connection, event: Mapping[str, object]
) -> None:
    cursor = connection.execute(
        """INSERT INTO events(provider, source_path, source_line,
           source_ordinal, source_hash, timestamp, cwd, repo, role, text)
           VALUES(:provider, :source_path, :source_line, :source_ordinal,
           :source_hash, :timestamp, :cwd, :repo, :role, :text)""",
        event,
    )
    if cursor.lastrowid is None:
        raise sqlite3.Error("event insert did not return a row id")
    connection.execute(
        "INSERT INTO event_fts(rowid, text) VALUES(?, ?)",
        (cursor.lastrowid, event["text"]),
    )
    if event["role"] == "user" and (value := _preference(str(event["text"]))):
        previous = connection.execute(
            "SELECT id FROM assertions WHERE kind = 'preference' "
            "AND state = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if previous:
            connection.execute(
                "UPDATE assertions SET state = 'superseded' WHERE id = ?",
                (previous[0],),
            )
        connection.execute(
            """INSERT INTO assertions(event_id, kind, value, state, supersedes,
               created_at) VALUES(?, 'preference', ?, 'active', ?, ?)""",
            (
                cursor.lastrowid,
                value,
                previous[0] if previous else None,
                time.time(),
            ),
        )


def advance_source(
    connection: sqlite3.Connection,
    provider: str,
    source_path: str,
    identity: str,
    fingerprint: str,
    events: Sequence[Mapping[str, object]],
    cursor_bytes: int,
    cursor_line: int,
    pending: bool,
    replace: bool,
) -> int:
    """Commit source events, FTS, assertions, and cursor as one transaction."""
    if replace:
        _delete_events(connection, provider, source_path)
    for event in events:
        _insert_event(connection, event)
    connection.execute(
        """INSERT INTO sources(provider, source_path, identity, fingerprint,
           cursor_bytes, cursor_line, pending, status, error, last_seen)
           VALUES(?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?)
           ON CONFLICT(provider, source_path) DO UPDATE SET
             identity=excluded.identity, fingerprint=excluded.fingerprint,
             cursor_bytes=excluded.cursor_bytes,
             cursor_line=excluded.cursor_line,
             pending=excluded.pending, status='active', error=NULL,
             last_seen=excluded.last_seen""",
        (
            provider,
            source_path,
            identity,
            fingerprint,
            cursor_bytes,
            cursor_line,
            int(pending),
            time.time(),
        ),
    )
    return len(events)


def mark_issue(
    connection: sqlite3.Connection,
    provider: str,
    source_path: str,
    identity: str,
    fingerprint: str,
    error: str,
) -> None:
    """Quarantine a dirty source while retaining rows for a later repair."""
    connection.execute(
        """INSERT INTO sources(provider, source_path, identity, fingerprint,
           cursor_bytes, cursor_line, pending, status, error, last_seen)
           VALUES(?, ?, ?, ?, 0, 0, 1, 'error', ?, ?)
           ON CONFLICT(provider, source_path) DO UPDATE SET
             identity=excluded.identity, fingerprint=excluded.fingerprint,
             pending=1, status='error', error=excluded.error,
             last_seen=excluded.last_seen""",
        (provider, source_path, identity, fingerprint, error, time.time()),
    )


def mark_missing(
    connection: sqlite3.Connection, current: set[tuple[str, str]]
) -> int:
    """Tombstone missing sources without silently erasing retained evidence."""
    changed = 0
    for provider, source_path in source_rows(connection):
        if (provider, source_path) not in current:
            cursor = connection.execute(
                "UPDATE sources SET status='missing', pending=1, "
                "error='source_missing', last_seen=? WHERE provider=? "
                "AND source_path=? AND status != 'missing'",
                (time.time(), provider, source_path),
            )
            changed += cursor.rowcount
    return changed


def erase_source(
    connection: sqlite3.Connection, provider: str, source_id: str
) -> bool:
    """Erase one source and linked assertions by its redacted identity."""
    for (path,) in connection.execute(
        "SELECT source_path FROM sources WHERE provider = ?", (provider,)
    ):
        if hashlib.sha256(path.encode()).hexdigest()[:16] == source_id:
            _delete_events(connection, provider, path)
            connection.execute(
                "DELETE FROM sources WHERE provider = ? AND source_path = ?",
                (provider, path),
            )
            return True
    return False


def checkpoint(
    connection: sqlite3.Connection, database: Path
) -> dict[str, object]:
    """Run a passive checkpoint and return redacted WAL measurements."""
    result = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    wal = database.with_name(f"{database.name}-wal")
    try:
        wal_bytes = wal.stat().st_size
    except OSError:
        wal_bytes = 0
    return {
        "journal_mode": "wal",
        "wal_bytes": wal_bytes,
        "checkpoint": list(result) if result else [],
        "checkpoint_at": time.time(),
    }
