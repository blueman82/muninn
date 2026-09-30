"""pctx store: schema v1, connections, writer lock, private files."""

import fcntl
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# Design 3.2 DDL plus the accepted amendments (event.cwd + event_cwd index,
# event.kind delegation/tool_error, event.parent_event_id). Line-wrapped to
# 79 columns; source_issue lists `code` before its table-level PRIMARY KEY
# because SQLite rejects a column definition after a table constraint.
# knowledge_ai/ad/au are spelled out with NULL-text guards: an erased entry
# (text NULL) is not in knowledge_fts, and an FTS5 'delete' for a row that is
# not indexed corrupts the index.
SCHEMA_SQL = r"""
CREATE TABLE scope (id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,  -- main-worktree realpath | bare cwd | 'global'
  label TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('git','dir','global')));
CREATE TABLE scope_path (cwd TEXT PRIMARY KEY,
  scope_id INTEGER NOT NULL REFERENCES scope(id),
  method TEXT NOT NULL
    CHECK (method IN ('git','worktree','prefix','cwd','alias')));
CREATE TABLE source (id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('codex','claude')),
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
    CHECK (root IN ('codex-sessions','codex-archived','claude-projects')),
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
  UNIQUE (provider, thread_id), UNIQUE (root, path));
CREATE INDEX source_session ON source(provider, session_root);
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
  cwd TEXT,                          -- exact cwd string in effect (A3)
  parent_event_id INTEGER,           -- tool_error -> its tool_call; no FK
  flags INTEGER NOT NULL DEFAULT 0,  -- 1 legacy-envelope marker, 2 redacted,
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
CREATE TABLE knowledge (id INTEGER PRIMARY KEY,  -- shown as K<id>
  scope_id INTEGER NOT NULL REFERENCES scope(id),  -- repo scope or 'global'
  kind TEXT NOT NULL
    CHECK (kind IN ('decision','fact','preference','procedure')),
  text TEXT,                 -- 1..500 chars, redacted; NULL only when erased
  status TEXT NOT NULL
    CHECK (status IN ('current','superseded','retracted','erased')),
  supersedes INTEGER REFERENCES knowledge(id),
  superseded_by INTEGER REFERENCES knowledge(id),
  retract_reason TEXT,       -- <=200 chars; NULL when erased
  actor TEXT NOT NULL,       -- 'user' | 'claude:<root12>' | 'codex:<root12>'
  created_at REAL NOT NULL,
  CHECK (status = 'erased' OR text IS NOT NULL));
CREATE VIRTUAL TABLE knowledge_fts USING fts5(text,
  content='knowledge', content_rowid='id');
INSERT INTO knowledge_fts(knowledge_fts, rank) VALUES ('secure-delete', 1);
CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
  INSERT INTO knowledge_fts(rowid, text)
    SELECT new.id, new.text WHERE new.text IS NOT NULL; END;
CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, text)
    SELECT 'delete', old.id, old.text WHERE old.text IS NOT NULL; END;
CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, text)
    SELECT 'delete', old.id, old.text WHERE old.text IS NOT NULL;
  INSERT INTO knowledge_fts(rowid, text)
    SELECT new.id, new.text WHERE new.text IS NOT NULL; END;
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
CREATE TABLE knowledge_log (id INTEGER PRIMARY KEY,
  knowledge_id INTEGER NOT NULL,  -- append-only, no free text
  action TEXT NOT NULL CHECK (action IN
    ('add','supersede','superseded','retract','erase')),
  actor TEXT NOT NULL, at REAL NOT NULL);
CREATE TRIGGER knowledge_log_no_update BEFORE UPDATE ON knowledge_log BEGIN
  SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER knowledge_log_no_delete BEFORE DELETE ON knowledge_log BEGIN
  SELECT RAISE(ABORT, 'append-only'); END;
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


class StoreUnavailable(Exception):
    """The store is absent, unreadable or not a schema this code speaks."""


class HotJournal(StoreUnavailable):
    """A crashed writer left a journal that only a writer can roll back."""


class Busy(Exception):
    """Another process holds the writer lock."""


def data_home(env: Mapping[str, str] = os.environ) -> Path:
    """$PCTX_HOME, else ~/.local/share/provenance-context."""
    configured = env.get("PCTX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "provenance-context"


def db_path(home: Path) -> Path:
    return home / "pctx.sqlite"


def ensure_private_dir(path: Path) -> None:
    """mkdir -p, then force mode 0700 on the leaf."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_json_atomic(path: Path, obj: object) -> None:
    """Write JSON to a 0600 temp file in the same dir, then os.replace."""
    payload = json.dumps(obj, sort_keys=True).encode("utf-8")  # may raise
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:  # mkstemp files are 0600
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


_WRITER_PRAGMAS = (
    "PRAGMA busy_timeout=5000",
    "PRAGMA secure_delete=ON",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=FULL",
    "PRAGMA cache_size=-262144",  # 256 MiB: big replaces must not spill
)


def _ensure_dir(path: Path) -> None:
    """Create a missing data dir privately; never touch an existing one."""
    if not path.is_dir():
        ensure_private_dir(path)


def _init_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise StoreUnavailable(f"schema v{version}, need v{SCHEMA_VERSION}")
    # All-or-nothing: DDL and user_version commit together. On failure the
    # transaction stays open and connect_rw's close() rolls it back.
    conn.executescript(
        f"BEGIN IMMEDIATE;{SCHEMA_SQL}"
        f"PRAGMA user_version={SCHEMA_VERSION};COMMIT;"
    )


def connect_rw(path: Path, fullfsync: bool = True) -> sqlite3.Connection:
    """Open the store for writing, creating and migrating it.

    Autocommit connection (callers issue BEGIN IMMEDIATE); rows are
    sqlite3.Row. fullfsync=False is only for a re-derivable pre-build.
    """
    _ensure_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)  # born private
    os.fchmod(fd, 0o600)  # tightened if it already existed
    os.close(fd)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        for pragma in _WRITER_PRAGMAS:
            conn.execute(pragma)
        conn.execute(f"PRAGMA fullfsync={'ON' if fullfsync else 'OFF'}")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode != "delete":
            raise StoreUnavailable(f"journal_mode is {mode!r}, need 'delete'")
        _init_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def writer_lock(home: Path, wait_s: float = 15.0) -> Iterator[None]:
    """Hold the flock on home/writer.lock; Busy after wait_s (0 = no wait).

    Not reentrant (a second open() in one process conflicts): take it once
    at command entry and never nest. Never fork while holding it.
    """
    _ensure_dir(home)
    fd = os.open(home / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:  # contention raises, it does not return
                if time.monotonic() >= deadline:
                    raise Busy("writer lock is held elsewhere") from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)  # closing releases the flock, also on SIGKILL


def _uri(path: Path, mode: str) -> str:
    return Path(path).absolute().as_uri() + f"?mode={mode}"


def connect_ro(path: Path) -> sqlite3.Connection:
    """Open the store read-only; StoreUnavailable if it cannot be read.

    Raises HotJournal when a crashed writer left a journal that this
    read-only connection cannot roll back (see heal_hot_journal).
    """
    conn = None
    try:
        conn = sqlite3.connect(
            _uri(path, "ro"), uri=True, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        code = getattr(exc, "sqlite_errorcode", None)
        if code == sqlite3.SQLITE_READONLY_ROLLBACK:
            raise HotJournal(
                "hot journal: a writer must roll it back"
            ) from exc
        name = getattr(exc, "sqlite_errorname", type(exc).__name__)
        raise StoreUnavailable(f"cannot read the store ({name})") from exc
    if version != SCHEMA_VERSION:
        conn.close()
        raise StoreUnavailable(f"schema v{version}, need v{SCHEMA_VERSION}")
    return conn


def heal_hot_journal(path: Path, home: Path) -> bool:
    """Roll back a crashed writer's journal; True once a writer opened it.

    Takes the writer lock without waiting and opens read-write once (the
    first read rolls the journal back). False when the lock is busy or this
    process may not write (sandboxed), so the caller reports hot_journal.
    """
    try:
        with writer_lock(home, wait_s=0):
            conn = sqlite3.connect(_uri(path, "rw"), uri=True)
            try:
                conn.execute("PRAGMA user_version").fetchone()
            finally:
                conn.close()
    except (Busy, OSError, sqlite3.Error):
        return False
    return True
