"""Ingest provider transcripts into the store.

Discover provider transcripts, identify each thread from line 1, and commit
its newline-terminated lines as events.  `ingest` needs the caller to hold
`store.writer_lock`; `run_pass` takes it.  There is one BEGIN IMMEDIATE per
source: events, cursor, anchor, parse state and usage counts commit
together.

Planning lives in `muninn.ingest_plan`, line parsing in `muninn.ingest_parse`
and the shared types in `muninn.ingest_model`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple, cast

from muninn import classify, store
from muninn.ingest_model import (
    INDEXED,
    PROVIDER,
    PassStats,
    PlanOptions,
    Work,
)
from muninn.ingest_parse import (
    ParseStart,
    Progress,
    Usage,
    parse_source,
)
from muninn.ingest_plan import (
    discover,
    late_forks,
    plan_source,
    source_id_for,
)
from muninn.tombstone_key import TombstoneKeyError

__all__ = ["PassStats", "default_roots", "ingest", "run_pass"]

# Progress of a source that is indexed as a row only (no events read).
_NO_PROGRESS = Progress((0, 0), (None, None), None, 0)


class _Written(NamedTuple):
    """The counts one source's transaction produced."""

    source_id: int
    added: int
    removed: int
    skipped: int


def default_roots(env: Mapping[str, str] = os.environ) -> dict[str, Path]:
    """Return the provider roots, or those named by MUNINN_ROOTS.

    MUNINN_ROOTS (a JSON object, root name to path) replaces the defaults,
    e.g. for tests.

    Args:
        env: Environment to read HOME and MUNINN_ROOTS from.

    Returns:
        Root name to directory; the directories may not exist.

    Raises:
        ValueError: If MUNINN_ROOTS is not an object naming known roots.
    """
    home = Path(env.get("HOME") or Path.home())
    raw = env.get("MUNINN_ROOTS")
    if not raw:
        return {
            "codex-sessions": home / ".codex" / "sessions",
            "codex-archived": home / ".codex" / "archived_sessions",
            "claude-projects": home / ".claude" / "projects",
        }
    given = json.loads(raw)
    # isinstance leaves the JSON object's types unknown; its keys are always
    # strings and every value is passed through str() below.
    roots = cast(dict[str, object], given)
    if not isinstance(given, dict) or not set(roots) <= set(PROVIDER):
        raise ValueError(f"MUNINN_ROOTS must map {sorted(PROVIDER)} to paths")
    return {name: _expand(str(path), home) for name, path in roots.items()}


def _expand(path: str, home: Path) -> Path:
    """Expand a leading ``~`` against ``home``, not the process's HOME."""
    if path == "~" or path.startswith("~/"):
        return home / path[2:]
    return Path(path)


def run_pass(
    home: Path,
    roots: Mapping[str, Path],
    *,
    only_threads: set[str] | None = None,
    full: bool = False,
    fullfsync: bool = True,
    wait_s: float = 0.0,
) -> PassStats:
    """Take the writer lock, open the store, run one ingest pass, close.

    store.BusyError propagates if the lock is still held after ``wait_s``,
    and ValueError if a root name is not a known provider.

    Args:
        home: Data home that holds the store and the lock.
        roots: Provider root name to directory.
        only_threads: Limit the pass to these thread or session ids.
        full: Re-read every source from the start.
        fullfsync: False only for a re-derivable pre-build.
        wait_s: Seconds to wait for the lock; the poller passes 0 and skips
            the pass when another writer is busy.

    Returns:
        Counters for the pass.
    """
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home), fullfsync=fullfsync)
        try:
            return ingest(conn, roots, only_threads=only_threads, full=full)
        finally:
            conn.close()


def _no_beat() -> None:
    """Do nothing: the default when nobody watches the pass."""


def ingest(
    conn: sqlite3.Connection,
    roots: Mapping[str, Path],
    *,
    only_threads: set[str] | None = None,
    full: bool = False,
    on_source: Callable[[], None] = _no_beat,
) -> PassStats:
    """Run one pass over the roots with a store.connect_rw connection.

    The caller must hold store.writer_lock for the whole call: writers are
    serialised by it, and the lock is not reentrant.  ValueError propagates
    if a root name is not a known provider.

    Args:
        conn: Read-write connection from store.connect_rw.
        roots: Provider root name to directory.
        only_threads: Limit the pass to these thread or session ids (a
            continuation file also matches its base thread id); this also
            skips missing-source marking, which needs a view of every root.
        full: Re-read every source from the start.
        on_source: Called after each source is planned or written, so a
            caller can show that a long pass is still alive.

    Returns:
        Counters for the pass.
    """
    started = time.monotonic()
    opts = PlanOptions(roots, full, only_threads)
    stats, seen, work = PassStats(), set[int](), list[Work]()
    for name, path, st in discover(roots):
        stats.files_seen += 1
        item = plan_source(conn, opts, stats, name, path, st)
        if isinstance(item, Work):
            work.append(item)
        elif item is not None:
            seen.add(item)  # an unchanged or skipped known source
        on_source()
    # Parents before their old-format forks: the content-prefix rule reads
    # the parent's events, so they must be written first.  Python's sort is
    # stable, so every other order (path order) is kept.
    work.sort(key=lambda w: w.info.replay_mode == "content_prefix")
    scopes: dict[str | None, int] = {}
    for item in work:
        _process(conn, item, stats, scopes, seen)
        on_source()
    for item in late_forks(conn, opts, stats):
        _process(conn, item, stats, scopes, seen)
        on_source()
    if only_threads is None:
        stats.missing = _mark_missing(conn, roots, seen)
    stats.duration_s = time.monotonic() - started
    return stats


def _mark_missing(
    conn: sqlite3.Connection, roots: Mapping[str, Path], seen: set[int]
) -> int:
    """Mark active sources under a scanned root that were not seen.

    Only the status changes: the events stay searchable, since a transcript
    that was archived or deleted elsewhere is still history.

    Returns:
        How many sources were newly marked missing.
    """
    marks = ",".join("?" * len(roots))
    rows = conn.execute(
        f"SELECT id FROM source WHERE status = 'active' AND root IN ({marks})",
        tuple(roots),
    )
    gone = [(r[0],) for r in rows if r[0] not in seen]
    if gone:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "UPDATE source SET status = 'missing' WHERE id = ?", gone
        )
        conn.execute("COMMIT")
    return len(gone)


def _process(
    conn: sqlite3.Connection,
    w: Work,
    stats: PassStats,
    scopes: dict[str | None, int],
    seen: set[int],
) -> None:
    """Write one source in one transaction; an error rolls back only it."""
    if w.row is not None:
        seen.add(w.row["id"])  # never marked missing because it failed
    elif source_id_for(conn, PROVIDER[w.name], w.info.thread_id):
        stats.skipped_files += 1  # a copy that arrived earlier this pass
        return
    try:
        # IMMEDIATE takes the write lock now rather than at the first write,
        # so a source never fails half-way on a lock upgrade.
        conn.execute("BEGIN IMMEDIATE")
        written = _write(conn, w, scopes)
        conn.execute("COMMIT")
    except Exception as exc:  # counted; the next pass redoes the source
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        # The cache may hold scope ids created inside the rolled-back
        # transaction; reusing one would violate the scope foreign key.
        scopes.clear()
        if isinstance(exc, TombstoneKeyError):
            # Not a bad source: every fork would fail the same way, and
            # only restoring the key helps, so stop the pass loudly.
            raise
        stats.failed += 1
        kind = type(exc).__name__
        stats.errors[kind] = stats.errors.get(kind, 0) + 1
        return
    seen.add(written.source_id)
    stats.files_changed += 1
    stats.events_added += written.added
    stats.events_removed += written.removed
    stats.skipped_lines += written.skipped


def _replay(
    conn: sqlite3.Connection, info: classify.ThreadInfo, provider: str
) -> tuple[int | None, str]:
    """Return (parent source id, replay mode) for a source about to be written.

    A content-prefix fork can only drop its copied history once its parent
    is indexed; until then it is recorded as 'unverified' and planned again
    when the parent arrives.
    """
    parent_id, replay_mode = None, info.replay_mode
    if replay_mode == "content_prefix":
        parent_id = source_id_for(conn, provider, info.forked_from_id)
        replay_mode = "content_prefix" if parent_id else "unverified"
    return parent_id, replay_mode


def _upsert_source(
    conn: sqlite3.Connection, w: Work, replay_mode: str, now: float
) -> int:
    """Insert the source row, or refresh its identity; return its id."""
    info, row = w.info, w.row
    ident = (
        info.session_root,
        info.parent_thread_id,
        info.forked_from_id,
        info.thread_class,
        info.class_reason,
        replay_mode,
        info.replay_before,
        w.name,
        w.rel,
        classify.record_hash(w.first),
        classify.CLASSIFIER_VERSION,
    )
    if row is not None:
        # root and path are refreshed too: a known thread may have moved
        # (an archive move) and keeps its row and events.
        conn.execute(
            "UPDATE source SET session_root=?, parent_thread_id=?,"
            " forked_from_id=?, thread_class=?, class_reason=?,"
            " replay_mode=?, replay_before=?, root=?, path=?,"
            " first_line_sha256=?, classifier_version=? WHERE id=?",
            (*ident, row["id"]),
        )
        return row["id"]
    new_id = conn.execute(
        "INSERT INTO source(session_root, parent_thread_id,"
        " forked_from_id, thread_class, class_reason, replay_mode,"
        " replay_before, root, path, first_line_sha256,"
        " classifier_version, provider, thread_id, ino, size, mtime_ns,"
        " first_seen, last_seen) VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
        (*ident, PROVIDER[w.name], info.thread_id, now, now),
    ).lastrowid
    # An INSERT that did not raise always sets lastrowid; typeshed types it
    # as Optional because it is None after other statement kinds.
    return cast(int, new_id)


def _clear_source(conn: sqlite3.Connection, source_id: int) -> int:
    """Delete a source's events, issues and usage; return events removed."""
    removed = conn.execute(
        "DELETE FROM event WHERE source_id = ?", (source_id,)
    ).rowcount
    for table in ("source_issue", "usage"):
        conn.execute(f"DELETE FROM {table} WHERE source_id = ?", (source_id,))
    return removed


def _write(
    conn: sqlite3.Connection, w: Work, scopes: dict[str | None, int]
) -> _Written:
    """Write one source inside the caller's transaction.

    The source row, the events, the cursor and the usage counts are all
    written here so that the caller's COMMIT makes them visible together.

    Returns:
        The source id and the counts the transaction produced.
    """
    info, row = w.info, w.row
    parent_id, replay_mode = _replay(conn, info, PROVIDER[w.name])
    now = time.time()
    source_id = _upsert_source(conn, w, replay_mode, now)
    resume = row is not None and w.mode == "append"
    removed = 0
    if row is not None and not resume:
        removed = _clear_source(conn, source_id)
    if info.thread_class not in INDEXED:  # a row with stat fields only
        _commit_source(conn, source_id, w.st, now, _NO_PROGRESS, reset=True)
        return _Written(source_id, 0, removed, 0)
    start = _start_of(row if resume else None, parent_id)
    result = parse_source(conn, w, source_id, start, scopes)
    _commit_source(
        conn, source_id, w.st, now, result.progress, reset=not resume
    )
    _add_usage(
        conn, source_id, PROVIDER[w.name], info.session_root, result.usage
    )
    return _Written(source_id, result.added, removed, result.progress.skipped)


def _start_of(prior: sqlite3.Row | None, parent_id: int | None) -> ParseStart:
    """Return where to begin parsing: the row's cursor, or the file start."""
    if prior is None:
        return ParseStart((0, 0), (None, None), None, parent_id)
    return ParseStart(
        (prior["cursor_bytes"], prior["cursor_line"]),
        (prior["anchor_offset"], prior["anchor_sha256"]),
        prior["parse_state"],
        parent_id,
    )


def _add_usage(
    conn: sqlite3.Connection,
    source_id: int,
    provider: str,
    session_root: str,
    usage: Usage,
) -> None:
    """Add this pass's muninn usage counts to the source's running totals."""
    if not (usage.calls or usage.errors):
        return
    conn.execute(
        "INSERT INTO usage(source_id, provider, session_root, calls,"
        " errors, last_ts) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(source_id) DO UPDATE SET"
        " calls = calls + excluded.calls,"
        " errors = errors + excluded.errors,"
        " last_ts = CASE WHEN excluded.last_ts > coalesce(last_ts, '')"
        " THEN excluded.last_ts ELSE last_ts END",
        (
            source_id,
            provider,
            session_root,
            usage.calls,
            usage.errors,
            usage.last_ts,
        ),
    )


def _commit_source(
    conn: sqlite3.Connection,
    source_id: int,
    st: os.stat_result,
    now: float,
    progress: Progress,
    *,
    reset: bool,
) -> None:
    """Record the cursor, anchor, stat fields and parse state of a source.

    They move in the same transaction as the events, so after a crash the
    cursor never points past an event that was not stored.

    Args:
        conn: Connection inside the source's write transaction.
        source_id: The source row to update.
        st: The stat taken when the file was listed.
        now: Timestamp for last_seen.
        progress: Cursor, anchor, parse state and skipped-line count.
        reset: True when the source was rebuilt, so skipped_lines is set
            rather than added to.
    """
    conn.execute(
        "UPDATE source SET ino=?, size=?, mtime_ns=?, cursor_bytes=?,"
        " cursor_line=?, anchor_offset=?, anchor_sha256=?, parse_state=?,"
        " status='active', last_seen=?,"
        " skipped_lines = CASE WHEN ? THEN ? ELSE skipped_lines + ? END"
        " WHERE id=?",
        (
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
            *progress.cursor,
            *progress.anchor,
            progress.state,
            now,
            reset,
            progress.skipped,
            progress.skipped,
            source_id,
        ),
    )
