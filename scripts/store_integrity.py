"""Check and record consistency of the disposable derived store."""

from __future__ import annotations

import sqlite3

COUNT_KEYS = {
    "events": "derived_event_count",
    "event_fts": "derived_fts_count",
    "assertions": "derived_assertion_count",
    "sources": "derived_source_count",
}


def store_counts(connection: sqlite3.Connection) -> None:
    """Record derived row counts in the same transaction as mutations."""
    connection.executemany(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [
            (
                key,
                str(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                ),
            )
            for table, key in COUNT_KEYS.items()
        ],
    )


def store_is_consistent(
    connection: sqlite3.Connection, verify_sqlite: bool
) -> bool:
    """Return whether source cursors and derived evidence agree exactly."""
    try:
        if verify_sqlite and connection.execute(
            "PRAGMA integrity_check"
        ).fetchone() != ("ok",):
            return False
        actual = {
            key: str(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                    0
                ]
            )
            for table, key in COUNT_KEYS.items()
        }
        expected = dict(
            connection.execute(
                "SELECT key, value FROM meta WHERE key IN (?, ?, ?, ?)",
                tuple(COUNT_KEYS.values()),
            )
        )
        if actual != expected:
            return False
        return not any(
            connection.execute(query).fetchone()[0]
            for query in (
                "SELECT COUNT(*) FROM event_fts "
                "WHERE rowid NOT IN (SELECT id FROM events)",
                "SELECT COUNT(*) FROM assertions "
                "WHERE event_id NOT IN (SELECT id FROM events)",
                "SELECT COUNT(*) FROM assertions WHERE supersedes IS NOT NULL "
                "AND supersedes NOT IN (SELECT id FROM assertions)",
            )
        )
    except sqlite3.Error:
        return False
