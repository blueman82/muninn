"""Store migration: schema v1 to v2, one transaction, one-way.

v2 widens the knowledge kind CHECK and adds typed columns.  SQLite cannot
alter a CHECK in place, so the table is rebuilt: create the new shape, copy
by explicit column list, drop the old table, rename, re-create the FTS
triggers.  Row ids are kept, so supersede chains, citations, the log and the
external-content knowledge_fts index stay valid without a reindex.
"""

from __future__ import annotations

import sqlite3

from muninn.store_schema import KNOWLEDGE_TABLE, KNOWLEDGE_TRIGGERS

# The v1 columns, in v1 order; the v2 additions take their defaults.
V1_KNOWLEDGE_COLUMNS = (
    "id, scope_id, kind, text, status, supersedes, superseded_by,"
    " retract_reason, actor, created_at"
)


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Rebuild the knowledge table in place as schema v2.

    All-or-nothing: the rebuild and ``user_version`` commit together, and a
    failure rolls back to an untouched v1 file.  A concurrent migrator that
    won the race is detected after the write lock is taken and left alone.

    Args:
        conn: Autocommit read-write connection (isolation_level None).
    """
    prior_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    # Cannot be changed inside a transaction; the swap needs it off so that
    # dropping the old table does not check its referrers.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 1:
                _swap_knowledge(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if prior_fk else 'OFF'}")


def _swap_knowledge(conn: sqlite3.Connection) -> None:
    """Run the create, copy, drop, rename steps inside the open transaction."""
    conn.execute(KNOWLEDGE_TABLE.format(name="knowledge_v2"))
    conn.execute(
        f"INSERT INTO knowledge_v2 ({V1_KNOWLEDGE_COLUMNS})"
        f" SELECT {V1_KNOWLEDGE_COLUMNS} FROM knowledge"
    )
    conn.execute("DROP TABLE knowledge")  # takes its three triggers with it
    conn.execute("ALTER TABLE knowledge_v2 RENAME TO knowledge")
    for trigger in KNOWLEDGE_TRIGGERS:
        conn.execute(trigger)
    conn.execute("PRAGMA user_version=2")
