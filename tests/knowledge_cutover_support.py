"""Pure fixture builders for strict governed copies and DB-only history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable

from muninn import store_schema


def digest(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, str]:
    """Compare every scalar and identity independently, in stable order."""
    result: dict[str, str] = {}
    for table in tables:
        rows = sorted(
            json.dumps(tuple(row))
            for row in conn.execute(f"SELECT * FROM {table}")
        )
        result[table] = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    return result


def legacy_schema() -> str:
    """Freeze the inventoried legacy composition without its old writer."""
    return (
        store_schema.SCOPE_SQL
        + store_schema._SOURCES_SQL
        + store_schema.KNOWLEDGE_TABLE.format(name="knowledge")
        + ";\n"
        + store_schema._FTS_SQL
        + ";\n".join(store_schema.KNOWLEDGE_TRIGGERS)
        + ";\n"
        + store_schema._CITATION_SQL
        + store_schema._LOG_SQL
        + store_schema._TOMBSTONE_SQL
        + "\nPRAGMA user_version=3;"
    )


def cursor_history(conn: sqlite3.Connection) -> None:
    """Build DB-only Cursor parentage, usage and parse diagnostics."""
    source = dict(conn.execute("SELECT * FROM source").fetchone())
    source.update(
        id=900,
        provider="cursor",
        thread_id="cursor-missing",
        session_root="cursor-missing",
        root="cursor-imports",
        path="missing.jsonl",
        status="missing",
    )
    conn.execute(
        f"INSERT INTO source VALUES({','.join('?' for _ in source)})",
        tuple(source.values()),
    )
    event = dict(conn.execute("SELECT * FROM event").fetchone())
    for ident, parent in ((900, None), (901, 900)):
        event.update(
            id=ident,
            source_id=900,
            line=ident,
            parent_event_id=parent,
            text=f"Cursor row {ident}",
        )
        conn.execute(
            f"INSERT INTO event VALUES({','.join('?' for _ in event)})",
            tuple(event.values()),
        )
    conn.execute(
        "INSERT INTO usage VALUES(900,'cursor','cursor-missing',3,1,NULL)"
    )
    conn.execute("INSERT INTO source_issue VALUES(900,99,1.0,'invalid_json')")
