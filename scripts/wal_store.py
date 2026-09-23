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

from store_integrity import COUNT_KEYS, store_counts, store_is_consistent
from wal_mutations import delete_events, failpoint, insert_event

SCHEMA_VERSION = "5"
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
            digest TEXT NOT NULL,
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
    connection.executemany(
        "INSERT OR IGNORE INTO meta(key, value) VALUES(?, '0')",
        [(key,) for key in COUNT_KEYS.values()],
    )


def needs_migration(database: Path, verify_sqlite: bool) -> bool:
    """Return whether the derived database is absent or not the WAL schema."""
    if not database.exists():
        return True
    try:
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            return row != (SCHEMA_VERSION,) or not store_is_consistent(
                connection, verify_sqlite
            )
    except sqlite3.Error:
        return True


def initialize(database: Path, rollback: Path, verify_sqlite: bool) -> bool:
    """Create checked storage, retaining a rollback copy before raw rebuild."""
    migrating = needs_migration(database, verify_sqlite)
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
            "digest": row[4],
            "cursor_bytes": row[5],
            "cursor_line": row[6],
            "pending": bool(row[7]),
            "status": row[8],
            "error": row[9],
        }
        for row in connection.execute(
            "SELECT provider, source_path, identity, fingerprint, digest, "
            "cursor_bytes, "
            "cursor_line, pending, status, error FROM sources"
        )
    }


def source_cursor(
    previous: Mapping[str, object] | None, identity: str, path: Path
) -> tuple[int, int, bool]:
    """Return the committed cursor only when an unchanged file was appended."""
    offset = previous.get("cursor_bytes") if previous else 0
    line = previous.get("cursor_line") if previous else 0
    valid_offset = offset if isinstance(offset, int) and offset >= 0 else 0
    valid_line = line if isinstance(line, int) and line >= 0 else 0
    append = bool(
        previous
        and previous["identity"] == identity
        and previous["status"] == "active"
        and not previous["pending"]
        and path.stat().st_size >= valid_offset
    )
    return (valid_offset, valid_line, append) if append else (0, 0, False)


def renamed_previous(
    connection: sqlite3.Connection,
    known: dict[tuple[str, str], dict[str, object]],
    provider: str,
    source_path: str,
    identity: str,
    fingerprint: str,
    digest: str | None,
) -> Mapping[str, object] | None:
    """Re-key one uniquely identified source after a path-only rename."""
    matches = [
        (key, row)
        for key, row in known.items()
        if key[0] == provider and row["identity"] == identity
    ]
    if len(matches) != 1:
        return None
    old_key, previous = matches[0]
    rename_source(
        connection,
        provider,
        old_key[1],
        source_path,
        fingerprint,
        digest or str(previous["digest"]),
    )
    known.pop(old_key)
    renamed = dict(previous) | {"fingerprint": fingerprint}
    known[(provider, source_path)] = renamed
    return renamed


def advance_source(
    connection: sqlite3.Connection,
    provider: str,
    source_path: str,
    identity: str,
    fingerprint: str,
    digest: str,
    events: Sequence[Mapping[str, object]],
    cursor_bytes: int,
    cursor_line: int,
    pending: bool,
    replace: bool,
) -> int:
    """Commit source events, FTS, assertions, and cursor as one transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        if replace:
            delete_events(connection, provider, source_path)
        for event in events:
            insert_event(connection, event)
        connection.execute(
            """INSERT INTO sources(provider, source_path, identity,
               fingerprint, digest, cursor_bytes, cursor_line, pending,
               status, error, last_seen)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?)
               ON CONFLICT(provider, source_path) DO UPDATE SET
                 identity=excluded.identity, fingerprint=excluded.fingerprint,
                 digest=excluded.digest,
                 cursor_bytes=excluded.cursor_bytes,
                 cursor_line=excluded.cursor_line,
                 pending=excluded.pending, status='active', error=NULL,
                 last_seen=excluded.last_seen""",
            (
                provider,
                source_path,
                identity,
                fingerprint,
                digest,
                cursor_bytes,
                cursor_line,
                int(pending),
                time.time(),
            ),
        )
        failpoint("after_source_state")
        store_counts(connection)
        failpoint("after_meta_counts")
        failpoint("before_commit")
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    return len(events)


def mark_issue(
    connection: sqlite3.Connection,
    provider: str,
    source_path: str,
    identity: str,
    fingerprint: str,
    digest: str,
    error: str,
) -> None:
    """Quarantine a dirty source while retaining rows for a later repair."""
    connection.execute(
        """INSERT INTO sources(provider, source_path, identity, fingerprint,
           digest, cursor_bytes, cursor_line, pending, status, error,
           last_seen)
           VALUES(?, ?, ?, ?, ?, 0, 0, 1, 'error', ?, ?)
           ON CONFLICT(provider, source_path) DO UPDATE SET
             identity=excluded.identity, fingerprint=excluded.fingerprint,
             digest=excluded.digest,
             pending=1, status='error', error=excluded.error,
             last_seen=excluded.last_seen""",
        (
            provider,
            source_path,
            identity,
            fingerprint,
            digest,
            error,
            time.time(),
        ),
    )
    store_counts(connection)


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


def rename_source(
    connection: sqlite3.Connection,
    provider: str,
    old_path: str,
    new_path: str,
    fingerprint: str,
    digest: str,
) -> None:
    """Re-key a moved source without retaining a false missing tombstone."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE events SET source_path = ? WHERE provider = ? "
            "AND source_path = ?",
            (new_path, provider, old_path),
        )
        failpoint("after_rename_events")
        connection.execute(
            "UPDATE sources SET source_path = ?, fingerprint = ?, digest = ?, "
            "last_seen = ? WHERE provider = ? AND source_path = ?",
            (new_path, fingerprint, digest, time.time(), provider, old_path),
        )
        failpoint("after_rename_source_state")
        store_counts(connection)
        failpoint("before_rename_commit")
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def erase_source(
    connection: sqlite3.Connection, provider: str, source_id: str
) -> bool:
    """Erase one source and linked assertions by its redacted identity."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        paths = connection.execute(
            "SELECT source_path FROM sources WHERE provider = ?", (provider,)
        )
        path = next(
            (
                candidate
                for (candidate,) in paths
                if hashlib.sha256(candidate.encode()).hexdigest()[:16]
                == source_id
            ),
            None,
        )
        if path is None:
            connection.commit()
            return False
        delete_events(connection, provider, path)
        connection.execute(
            "DELETE FROM sources WHERE provider = ? AND source_path = ?",
            (provider, path),
        )
        store_counts(connection)
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    return True


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
    checkpoint_data = list(result) if result else []
    derived_counts = {
        table: int(
            connection.execute(
                "SELECT value FROM meta WHERE key = ?", (COUNT_KEYS[table],)
            ).fetchone()[0]
        )
        for table in ("events", "assertions", "sources")
    }
    return {
        "journal_mode": "wal",
        "wal_bytes": wal_bytes,
        "checkpoint": checkpoint_data,
        "checkpoint_busy": bool(checkpoint_data and checkpoint_data[0]),
        "checkpoint_log_frames": (
            checkpoint_data[1] if len(checkpoint_data) > 1 else 0
        ),
        "checkpointed_frames": (
            checkpoint_data[2] if len(checkpoint_data) > 2 else 0
        ),
        "checkpoint_at": time.time(),
        "derived_counts": derived_counts,
    }
