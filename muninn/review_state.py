"""Store additive learning setup and safe recovery metadata."""

from __future__ import annotations

import sqlite3
from typing import Literal

from muninn.knowledge_models import Digest, StrictModel, Text, UtcSeconds

STATE_SQL = """
CREATE TABLE IF NOT EXISTS learning_setup (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 mode TEXT NOT NULL CHECK(mode IN ('now','all')),
 project_id TEXT, selected_at REAL NOT NULL, scan_digest TEXT);
CREATE TABLE IF NOT EXISTS review_issue (
 id TEXT PRIMARY KEY, code TEXT NOT NULL, stage TEXT NOT NULL,
 cause TEXT NOT NULL, action TEXT NOT NULL, created_at REAL NOT NULL);
"""


class LearningSelection(StrictModel):
    """Bind an explicit learning choice to its source scan and time."""

    mode: Literal["now", "all"]
    project_id: Text | None
    selected_at: UtcSeconds
    scan_digest: Digest | None


def ensure(conn: sqlite3.Connection) -> None:
    """Create only additive state tables in the caller's transaction."""
    if not conn.in_transaction:
        raise ValueError("caller_transaction_required")
    for statement in STATE_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)


def available(conn: sqlite3.Connection, table: str) -> bool:
    """Check optional state without modifying an older store."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def selection(conn: sqlite3.Connection) -> LearningSelection | None:
    """Read the saved choice without creating state on an old store."""
    if not available(conn, "learning_setup"):
        return None
    row = conn.execute(
        "SELECT * FROM learning_setup WHERE singleton=1"
    ).fetchone()
    return (
        None
        if row is None
        else LearningSelection.model_validate(
            {
                key: row[key]
                for key in ("mode", "project_id", "selected_at", "scan_digest")
            }
        )
    )


def baseline(
    conn: sqlite3.Connection, source: sqlite3.Row, default: int
) -> int:
    """Exclude old or undated records when a source is indexed after setup."""
    saved = selection(conn)
    if saved is None or saved.mode != "now":
        return default
    row = conn.execute(
        "SELECT max(line) FROM event WHERE source_id=? AND "
        "(julianday(ts) IS NULL OR julianday(ts)<=julianday(?,'unixepoch'))",
        (source["id"], saved.selected_at),
    ).fetchone()
    return max(default, row[0] or 0)
