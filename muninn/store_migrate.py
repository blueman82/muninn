"""Store migrations: one-way schema changes in transactions.

v2 widens the knowledge kind CHECK and adds typed columns.  SQLite cannot
alter a CHECK in place, so the table is rebuilt: create the new shape, copy
by explicit column list, drop the old table, rename, re-create the FTS
triggers.  Row ids are kept, so supersede chains, citations, the log and the
external-content knowledge_fts index stay valid without a reindex.
"""

from __future__ import annotations

import contextlib
import sqlite3

from muninn.store_schema import KNOWLEDGE_TABLE, KNOWLEDGE_TRIGGERS

# The v1 columns, in v1 order; the v2 additions take their defaults.
V1_KNOWLEDGE_COLUMNS = (
    "id, scope_id, kind, text, status, supersedes, superseded_by,"
    " retract_reason, actor, created_at"
)


def migrate_v1_to_v2(conn: sqlite3.Connection) -> int | None:
    """Rebuild the knowledge table in place as schema v2.

    All-or-nothing: the rebuild and ``user_version`` commit together, and a
    failure rolls back to an untouched v1 file.  A concurrent migrator that
    won the race is detected after the write lock is taken and left alone.

    Args:
        conn: Autocommit read-write connection (isolation_level None).

    Returns:
        The number of knowledge rows copied, or None when another migrator
        had already won the race.
    """
    copied: int | None = None
    prior_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    # Cannot be changed inside a transaction; the swap needs it off so that
    # dropping the old table does not check its referrers.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 1:
                copied = _swap_knowledge(conn)
            conn.execute("COMMIT")
        except BaseException:
            # A failing ROLLBACK must not replace the error that caused it.
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
    finally:
        # Same reason: restoring the pragma must not mask the real failure.
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"PRAGMA foreign_keys={'ON' if prior_fk else 'OFF'}")
    return copied


def migrate_v2_to_v3(conn: sqlite3.Connection) -> bool:
    """Widen provider and root checks while preserving indexed rows.

    Args:
        conn: Autocommit read-write connection.

    Returns:
        Whether this connection performed the migration.
    """
    prior_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] != 2:
                conn.execute("COMMIT")
                return False
            conn.execute(_SOURCE_V3)
            conn.execute("INSERT INTO source_v3 SELECT * FROM source")
            conn.execute(_USAGE_V3)
            conn.execute("INSERT INTO usage_v3 SELECT * FROM usage")
            conn.execute("DROP TABLE usage")
            conn.execute("DROP TABLE source")
            conn.execute("ALTER TABLE source_v3 RENAME TO source")
            conn.execute("ALTER TABLE usage_v3 RENAME TO usage")
            conn.execute(
                "CREATE INDEX source_session ON source(provider, session_root)"
            )
            conn.execute("PRAGMA user_version=3")
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"PRAGMA foreign_keys={'ON' if prior_fk else 'OFF'}")
    return True


_SOURCE_V3 = r"""CREATE TABLE source_v3 (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('codex','claude','cursor')),
  thread_id TEXT NOT NULL, session_root TEXT NOT NULL,
  parent_thread_id TEXT, forked_from_id TEXT,
  thread_class TEXT NOT NULL
    CHECK (thread_class IN ('primary','subagent','reviewer','other')),
  class_reason TEXT NOT NULL,
  replay_mode TEXT NOT NULL CHECK (replay_mode IN
    ('none','ordinal','history_base','content_prefix','unverified')),
  replay_before INTEGER,
  root TEXT NOT NULL CHECK (root IN
    ('codex-sessions','codex-archived','claude-projects','cursor-imports')),
  path TEXT NOT NULL, first_line_sha256 TEXT NOT NULL,
  ino INTEGER NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  cursor_bytes INTEGER NOT NULL DEFAULT 0,
  cursor_line INTEGER NOT NULL DEFAULT 0,
  anchor_offset INTEGER, anchor_sha256 TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','missing')),
  skipped_lines INTEGER NOT NULL DEFAULT 0,
  classifier_version INTEGER NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, parse_state TEXT,
  UNIQUE (provider, thread_id), UNIQUE (root, path))"""

_USAGE_V3 = r"""CREATE TABLE usage_v3 (
  source_id INTEGER PRIMARY KEY REFERENCES source(id) ON DELETE CASCADE,
  provider TEXT NOT NULL CHECK (provider IN ('codex','claude','cursor')),
  session_root TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0, last_ts TEXT)"""


def _violations(conn: sqlite3.Connection) -> int:
    """Count the rows ``PRAGMA foreign_key_check`` reports."""
    return len(conn.execute("PRAGMA foreign_key_check").fetchall())


def _swap_knowledge(conn: sqlite3.Connection) -> int:
    """Run the create, copy, drop, rename steps inside the open transaction.

    Before the swap can commit the copy is checked: the new table must hold
    as many rows as the old one and the swap must add no foreign key
    violation (one that was already there is not the migration's to fix).

    Returns:
        The number of rows copied.

    Raises:
        sqlite3.IntegrityError: If rows were lost or a link was broken; the
            caller rolls the whole swap back.
    """
    before = _violations(conn)
    wanted = conn.execute("SELECT count(*) FROM knowledge").fetchone()[0]
    conn.execute(KNOWLEDGE_TABLE.format(name="knowledge_v2"))
    conn.execute(
        f"INSERT INTO knowledge_v2 ({V1_KNOWLEDGE_COLUMNS})"
        f" SELECT {V1_KNOWLEDGE_COLUMNS} FROM knowledge"
    )
    copied = conn.execute("SELECT count(*) FROM knowledge_v2").fetchone()[0]
    if copied != wanted:
        raise sqlite3.IntegrityError("migration copied the wrong row count")
    conn.execute("DROP TABLE knowledge")  # takes its three triggers with it
    conn.execute("ALTER TABLE knowledge_v2 RENAME TO knowledge")
    for trigger in KNOWLEDGE_TRIGGERS:
        conn.execute(trigger)
    if _violations(conn) > before:
        raise sqlite3.IntegrityError("migration broke a foreign key")
    conn.execute("PRAGMA user_version=2")
    return int(copied)
