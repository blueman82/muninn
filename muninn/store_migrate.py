"""Store migration: schema v1 to v2, one transaction, one-way.

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
