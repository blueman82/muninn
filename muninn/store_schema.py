"""Store schema v3: the DDL a fresh store gets, in reusable pieces.

Schema v2 adds typed ledger fields to the knowledge table.  Its kind CHECK
cannot be altered in place, so ``store_migrate`` rebuilds the table from the
same ``KNOWLEDGE_TABLE`` template used here (``{name}`` is the table name);
one definition serves both paths.  The DDL is line-wrapped to 79 columns.
source_issue lists `code` before its table-level PRIMARY KEY because SQLite
rejects a column definition that follows a table constraint.  The
knowledge_ai/ad/au triggers are spelled out with NULL-text guards: an erased
entry (text NULL) is not in knowledge_fts, and an FTS5 'delete' for a row
that is not indexed corrupts the index.
"""

from __future__ import annotations

SCHEMA_VERSION = 3

SCOPE_SQL = r"""
CREATE TABLE scope (id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,  -- main-worktree realpath | bare cwd | 'global'
                             -- | 'loop:<id>' (kind 'dir')
  label TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('git','dir','global')));
"""

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

# Order matters: knowledge_ai/ad/au, recreated after a knowledge rebuild.
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

_SOURCES_SQL = r"""
CREATE TABLE scope_path (cwd TEXT PRIMARY KEY,
  scope_id INTEGER NOT NULL REFERENCES scope(id),
  method TEXT NOT NULL
    CHECK (method IN ('git','worktree','prefix','cwd','alias')));
CREATE TABLE source (id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('codex','claude','cursor')),
  thread_id TEXT NOT NULL,     -- codex line-1 payload.id | claude sessionId
  session_root TEXT NOT NULL,  -- codex payload.session_id | claude sessionId
  parent_thread_id TEXT, forked_from_id TEXT,
  thread_class TEXT NOT NULL
    CHECK (thread_class IN ('primary','subagent','reviewer','other')),
  class_reason TEXT NOT NULL,  -- 'thread_source=user' | 'path:subagents' | ...
  replay_mode TEXT NOT NULL CHECK (replay_mode IN
    ('none','ordinal','history_base','content_prefix','unverified')),
  replay_before INTEGER,       -- codex subagent_history_start_ordinal
  root TEXT NOT NULL
    CHECK (root IN ('codex-sessions','codex-archived','claude-projects',
                    'cursor-imports')),
  path TEXT NOT NULL,          -- relative to root; mutable (archive moves)
  first_line_sha256 TEXT NOT NULL, ino INTEGER NOT NULL,
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  cursor_bytes INTEGER NOT NULL DEFAULT 0,
  cursor_line INTEGER NOT NULL DEFAULT 0,  -- after last \n-terminated line
  anchor_offset INTEGER, anchor_sha256 TEXT,  -- last committed line
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','missing')),
  skipped_lines INTEGER NOT NULL DEFAULT 0,
  classifier_version INTEGER NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  parse_state TEXT,  -- ingest resume state (JSON ids and paths, no text)
  UNIQUE (provider, thread_id), UNIQUE (root, path));
CREATE INDEX source_session ON source(provider, session_root);
CREATE TABLE usage (  -- muninn invocations seen at ingest; counts only
  source_id INTEGER PRIMARY KEY REFERENCES source(id) ON DELETE CASCADE,
  provider TEXT NOT NULL CHECK (provider IN ('codex','claude','cursor')),
  session_root TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,  -- codex outputs with a failed exit
  last_ts TEXT);
CREATE TABLE source_issue (
  source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
  line INTEGER NOT NULL, at REAL NOT NULL,  -- codes only, never text
  code TEXT NOT NULL
    CHECK (code IN ('line_too_large','invalid_json','too_deep','not_object')),
  PRIMARY KEY (source_id, line));
CREATE TABLE event (id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES source(id),
  line INTEGER NOT NULL, part INTEGER NOT NULL,  -- 1-based JSONL line; 1-based
                                                 -- event index within the line
  byte_offset INTEGER NOT NULL,      -- start of the line in the file
  line_sha256 TEXT NOT NULL,         -- sha256(line w/o \r\n): record_hash
  seq INTEGER NOT NULL,              -- codex record 'ordinal' (else line)
  ts TEXT,                           -- provider UTC timestamp
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  kind TEXT NOT NULL CHECK (kind IN
    ('prompt','reply','tool_call','harness','delegation','tool_error')),
  tag TEXT,                          -- harness tag or tool name
  scope_id INTEGER NOT NULL REFERENCES scope(id),
  cwd TEXT,                          -- exact cwd string in effect
  parent_event_id INTEGER,           -- tool_error -> its tool_call; no FK
  flags INTEGER NOT NULL DEFAULT 0,  -- 1 injected-block marker, 2 redacted,
                                     -- 4 truncated (>64 KiB)
  text TEXT NOT NULL, UNIQUE (source_id, line, part));
CREATE INDEX event_scope_ts ON event(scope_id, ts);
CREATE INDEX event_cwd ON event(cwd);
CREATE VIRTUAL TABLE event_fts USING fts5(text,
  content='event', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
INSERT INTO event_fts(event_fts, rank) VALUES ('secure-delete', 1);
CREATE TRIGGER event_ai AFTER INSERT ON event BEGIN
  INSERT INTO event_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER event_ad AFTER DELETE ON event BEGIN
  INSERT INTO event_fts(event_fts, rowid, text)
    VALUES ('delete', old.id, old.text); END;
CREATE TRIGGER event_immutable BEFORE UPDATE ON event BEGIN
  SELECT RAISE(ABORT, 'event is immutable'); END;
"""

_FTS_SQL = r"""
CREATE VIRTUAL TABLE knowledge_fts USING fts5(text,
  content='knowledge', content_rowid='id');
INSERT INTO knowledge_fts(knowledge_fts, rank) VALUES ('secure-delete', 1);
"""

_CITATION_SQL = r"""
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

_TOMBSTONE_SQL = r"""
CREATE TABLE tombstone (id INTEGER PRIMARY KEY,
  created_at REAL NOT NULL, provider TEXT NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('session','thread','line')),
  session_root TEXT, thread_id TEXT, line INTEGER, line_sha256 TEXT,
  CHECK ((level = 'session' AND session_root IS NOT NULL)
      OR (level = 'thread' AND thread_id IS NOT NULL)
      OR (level = 'line' AND thread_id IS NOT NULL AND line IS NOT NULL
          AND line_sha256 IS NOT NULL)));
CREATE INDEX tombstone_session ON tombstone(provider, session_root);
CREATE INDEX tombstone_thread ON tombstone(provider, thread_id);
"""

_LOG_SQL = r"""
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

SCHEMA_SQL = (
    SCOPE_SQL
    + _SOURCES_SQL
    + KNOWLEDGE_TABLE.format(name="knowledge")
    + ";\n"
    + _FTS_SQL
    + ";\n".join(KNOWLEDGE_TRIGGERS)
    + ";\n"
    + _CITATION_SQL
    + _LOG_SQL
    + _TOMBSTONE_SQL
)
