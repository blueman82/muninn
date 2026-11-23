"""Pure fixture builders for strict governed copies and DB-only history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable

from muninn import store_schema

# The legacy knowledge DDL. Only these fixtures build a legacy store.
KNOWLEDGE_TABLE = r"""CREATE TABLE {name} (
  id INTEGER PRIMARY KEY,    -- shown as K<id>
  scope_id INTEGER NOT NULL REFERENCES scope(id),  -- repo, loop or 'global'
  kind TEXT NOT NULL CHECK (kind IN
    ('decision','fact','preference','procedure','lesson','constraint')),
  text TEXT,                 -- 1..500 chars, redacted; NULL only when erased
  status TEXT NOT NULL
    CHECK (status IN ('current','superseded','retracted','erased')),
  supersedes INTEGER REFERENCES knowledge(id),
  superseded_by INTEGER REFERENCES knowledge(id),
  retract_reason TEXT,       -- <=200 chars; NULL when erased
  actor TEXT NOT NULL,       -- 'user' | 'claude:<root12>' | 'codex:<root12>'
  created_at REAL NOT NULL,
  confidence TEXT            -- NULL: not stated
    CHECK (confidence IS NULL OR confidence IN
      ('observed','reported','inferred')),
  valid_until REAL,          -- epoch seconds; NULL never expires
  sensitivity TEXT NOT NULL DEFAULT 'normal'
    CHECK (sensitivity IN ('normal','restricted')),
  contradicts INTEGER REFERENCES knowledge(id),  -- informational only
  tags TEXT,                 -- comma-joined [a-z0-9_-]; data, not in FTS
  CHECK (status = 'erased' OR text IS NOT NULL))"""

KNOWLEDGE_TRIGGERS = (
    r"""
CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
  INSERT INTO knowledge_fts(rowid, text)
    SELECT new.id, new.text WHERE new.text IS NOT NULL; END""",
    r"""
CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, text)
    SELECT 'delete', old.id, old.text WHERE old.text IS NOT NULL; END""",
    r"""
CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, text)
    SELECT 'delete', old.id, old.text WHERE old.text IS NOT NULL;
  INSERT INTO knowledge_fts(rowid, text)
    SELECT new.id, new.text WHERE new.text IS NOT NULL; END""",
)

FTS_SQL = r"""
CREATE VIRTUAL TABLE knowledge_fts USING fts5(text,
  content='knowledge', content_rowid='id');
INSERT INTO knowledge_fts(knowledge_fts, rank) VALUES ('secure-delete', 1);
"""

CITATION_SQL = r"""
CREATE TABLE citation (id INTEGER PRIMARY KEY,
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id),
  provider TEXT NOT NULL, thread_id TEXT NOT NULL,
  line INTEGER NOT NULL, part INTEGER NOT NULL,
  line_sha256 TEXT NOT NULL,  -- identity copied at write time: survives
                              -- re-parse (event ids do not)
  role TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT,
  quote TEXT, span_start INTEGER, span_end INTEGER,  -- verbatim 12..300-char
                                                     -- span; NULL when erased
  state TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live','erased')));
CREATE INDEX citation_target ON citation(provider, thread_id, line, part);
"""

LOG_SQL = r"""
CREATE TABLE knowledge_log (id INTEGER PRIMARY KEY,
  knowledge_id INTEGER NOT NULL,  -- append-only, no free text
  action TEXT NOT NULL CHECK (action IN
    ('add','supersede','superseded','retract','erase')),
  actor TEXT NOT NULL, at REAL NOT NULL);
CREATE TRIGGER knowledge_log_no_update BEFORE UPDATE ON knowledge_log BEGIN
  SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER knowledge_log_no_delete BEFORE DELETE ON knowledge_log BEGIN
  SELECT RAISE(ABORT, 'append-only'); END;
"""


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
        + KNOWLEDGE_TABLE.format(name="knowledge")
        + ";\n"
        + FTS_SQL
        + ";\n".join(KNOWLEDGE_TRIGGERS)
        + ";\n"
        + CITATION_SQL
        + LOG_SQL
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
