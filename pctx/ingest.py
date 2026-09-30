"""pctx ingest (design 4.1, 3.5, 3.8; spec O1, O2, O7, O9, A3).

Discover provider transcripts, identify each thread from line 1, and commit
its newline-terminated lines as events.  ingest() needs the caller to hold
store.writer_lock; run_pass() takes it.  One BEGIN IMMEDIATE per source:
events, cursor, anchor, parse state and usage counts commit together.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pctx import classify, scope, store

MAX_LINE_BYTES = 8 * 1024 * 1024
PROVIDER = {
    "codex-sessions": "codex",
    "codex-archived": "codex",
    "claude-projects": "claude",
}
PATTERN = {
    "codex-sessions": "rollout-*.jsonl",
    "codex-archived": "rollout-*.jsonl",
    "claude-projects": "*.jsonl",
}
INDEXED = ("primary", "subagent")  # O2; reviewer and other: row only


@dataclass
class PassStats:
    files_seen: int = 0
    files_changed: int = 0
    events_added: int = 0
    events_removed: int = 0
    skipped_lines: int = 0
    missing: int = 0
    duration_s: float = 0.0
    skipped_files: int = 0  # tombstoned, unidentifiable or duplicate files
    failed: int = 0  # sources rolled back by an error; redone next pass
    errors: dict[str, int] = field(default_factory=dict)  # class name: n


def default_roots(env: Mapping[str, str] = os.environ) -> dict[str, Path]:
    """Provider roots; PCTX_ROOTS (a JSON object, root name -> path)
    replaces them, e.g. for tests and the trial mirror."""
    home = Path(env.get("HOME") or Path.home())
    raw = env.get("PCTX_ROOTS")
    if not raw:
        return {
            "codex-sessions": home / ".codex" / "sessions",
            "codex-archived": home / ".codex" / "archived_sessions",
            "claude-projects": home / ".claude" / "projects",
        }
    given = json.loads(raw)
    if not isinstance(given, dict) or not set(given) <= set(PROVIDER):
        raise ValueError(f"PCTX_ROOTS must map {sorted(PROVIDER)} to paths")
    return {name: _expand(str(path), home) for name, path in given.items()}


def _expand(path: str, home: Path) -> Path:
    if path == "~" or path.startswith("~/"):
        return home / path[2:]
    return Path(path)


def run_pass(
    home: Path,
    roots: dict[str, Path],
    *,
    only_threads: set[str] | None = None,
    full: bool = False,
    fullfsync: bool = True,
    wait_s: float = 0.0,
) -> PassStats:
    """Take the writer lock (store.Busy if held; the poller passes 0 and
    skips the pass), open the store, run one ingest pass, close."""
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home), fullfsync=fullfsync)
        try:
            return ingest(conn, roots, only_threads=only_threads, full=full)
        finally:
            conn.close()


@dataclass
class _Work:
    name: str
    path: Path
    rel: str
    st: os.stat_result
    info: classify.ThreadInfo
    row: sqlite3.Row | None
    first: bytes
    mode: str  # new | append | replace


def ingest(
    conn: sqlite3.Connection,
    roots: dict[str, Path],
    *,
    only_threads: set[str] | None = None,
    full: bool = False,
) -> PassStats:
    """One pass over the roots with a store.connect_rw connection.

    The caller must hold store.writer_lock for the whole call (writers are
    serialised by it; the lock is not reentrant).  only_threads limits the
    pass to those thread ids and skips missing-source marking.
    """
    started = time.monotonic()
    stats, seen, work = PassStats(), set(), []
    for name, path, st in _discover(roots, only_threads):
        stats.files_seen += 1
        item = _plan(conn, roots, name, path, st, full, only_threads, stats)
        if isinstance(item, _Work):
            work.append(item)
        elif item is not None:
            seen.add(item)  # an unchanged or skipped known source
    # Parents before their old-format forks: the content-prefix rule reads
    # the parent's events (design 3.5 #3).  The sort is stable.
    work.sort(key=lambda w: w.info.replay_mode == "content_prefix")
    scopes: dict[str | None, int] = {}
    for item in work:
        _process(conn, item, stats, scopes, seen)
    for item in _late_forks(conn, roots, only_threads, stats):
        _process(conn, item, stats, scopes, seen)
    if only_threads is None:
        stats.missing = _mark_missing(conn, roots, seen)
    stats.duration_s = time.monotonic() - started
    return stats


def _late_forks(conn, roots, only_threads, stats) -> Iterator[_Work]:
    """Unchanged 'unverified' old forks whose parent arrived this pass."""
    rows = conn.execute(
        "SELECT root, path FROM source WHERE replay_mode = 'unverified'"
    ).fetchall()
    for row in rows:
        if row["root"] not in roots:
            continue
        path = roots[row["root"]] / row["path"]
        try:
            st = os.lstat(path)
        except OSError:
            continue
        item = _plan(
            conn, roots, row["root"], path, st, False, only_threads, stats
        )
        if isinstance(item, _Work):
            yield item


def _discover(
    roots: dict[str, Path], only_threads: set[str] | None
) -> Iterator[tuple[str, Path, os.stat_result]]:
    """Regular files under each root in path order; symlinks, FIFOs and
    directories are never opened (rglob does not follow symlinked dirs)."""
    for name, root in roots.items():
        if name not in PROVIDER:
            raise ValueError(f"unknown root {name!r}")
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(PATTERN[name])):
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if only_threads and not any(
                path.stem.endswith(tid) for tid in only_threads
            ):
                continue  # codex names end in the id; claude stem = id
            yield name, path, st


def _plan(conn, roots, name, path, st, full, only_threads, stats):
    """A _Work item, a known source id to count as seen, or None."""
    rel = path.relative_to(roots[name]).as_posix()
    row = conn.execute(
        "SELECT * FROM source WHERE root = ? AND path = ?", (name, rel)
    ).fetchone()
    if row is not None and not full and _unchanged(conn, row, st):
        if row["status"] == "missing":  # back, byte-identical
            _reactivate(conn, row["id"])
        return row["id"]  # the cheap path reads nothing (design 4.1 #3)
    first = _first_line(path)
    info = _identify(name, roots[name], path, first) if first else None
    if info is None:  # line 1 incomplete, oversize or not a thread header
        stats.skipped_files += 1
        return row["id"] if row is not None else None
    if only_threads and info.thread_id not in only_threads:
        return None
    provider = PROVIDER[name]
    if _tombstoned(conn, provider, info):  # before any line is parsed
        stats.skipped_files += 1
        return None
    if row is None:
        row = conn.execute(
            "SELECT * FROM source WHERE provider = ? AND thread_id = ?",
            (provider, info.thread_id),
        ).fetchone()
        if row is not None and _still_there(roots, row):
            stats.skipped_files += 1  # a copy is not indexed twice (3.5 #5)
            return None
        # else: a rename or archive move; _process updates root and path
    elif row["thread_id"] != info.thread_id:
        stats.skipped_files += 1  # another thread under a known name
        return row["id"]
    mode = _mode(conn, row, info, first, st, path, full)
    return _Work(name, path, rel, st, info, row, first, mode)


def _unchanged(conn, row, st) -> bool:
    return (
        (row["ino"], row["size"], row["mtime_ns"])
        == (st.st_ino, st.st_size, st.st_mtime_ns)
        and row["classifier_version"] == classify.CLASSIFIER_VERSION
        and not _parent_arrived(conn, row)
    )


def _parent_arrived(conn, row) -> bool:
    """An 'unverified' old fork whose parent is now indexed (3.5 #3)."""
    return row["replay_mode"] == "unverified" and bool(
        _source_id(conn, row["provider"], row["forked_from_id"])
    )


def _mode(conn, row, info, first, st, path, full) -> str:
    if row is None:
        return "new"
    changed = (
        full
        or row["classifier_version"] != classify.CLASSIFIER_VERSION
        or info.thread_class not in INDEXED
        or _parent_arrived(conn, row)
        or classify.record_hash(first) != row["first_line_sha256"]
        or st.st_size < row["cursor_bytes"]  # truncated
        or (
            row["anchor_offset"] is not None
            and _hash_at(path, row["anchor_offset"]) != row["anchor_sha256"]
        )
    )
    return "replace" if changed else "append"


def _first_line(path: Path) -> bytes | None:
    """Line 1 with its newline, or None while incomplete or oversize."""
    with open(path, "rb") as handle:
        raw = handle.readline(MAX_LINE_BYTES + 1)
    return raw if raw.endswith(b"\n") else None


def _hash_at(path: Path, offset: int) -> str | None:
    with open(path, "rb") as handle:
        handle.seek(offset)
        raw = handle.readline(MAX_LINE_BYTES + 1)
    return classify.record_hash(raw) if raw.endswith(b"\n") else None


def _identify(name, root, path, first) -> classify.ThreadInfo | None:
    try:
        record = json.loads(first)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    if PROVIDER[name] == "claude":
        rel = path.relative_to(root).as_posix()
        return classify.claude_thread(rel, record)
    try:
        return classify.codex_thread(record)
    except ValueError:  # not a session_meta with an id
        return None


def _source_id(conn, provider, thread_id) -> int | None:
    if not thread_id:
        return None
    row = conn.execute(
        "SELECT id FROM source WHERE provider = ? AND thread_id = ?",
        (provider, thread_id),
    ).fetchone()
    return row[0] if row else None


def _tombstoned(conn, provider, info) -> bool:
    """Thread tombstone, or a session tombstone on this thread's session or
    on the session it was forked from (design 3.8)."""
    sessions = [info.session_root]
    if info.forked_from_id:
        sessions.append(info.forked_from_id)
        parent = conn.execute(
            "SELECT session_root FROM source WHERE provider = ?"
            " AND thread_id = ?",
            (provider, info.forked_from_id),
        ).fetchone()
        if parent:
            sessions.append(parent[0])
    marks = ",".join("?" * len(sessions))
    return (
        conn.execute(
            "SELECT 1 FROM tombstone WHERE provider = ? AND ("
            "(level = 'thread' AND thread_id = ?) OR "
            f"(level = 'session' AND session_root IN ({marks}))) LIMIT 1",
            (provider, info.thread_id, *sessions),
        ).fetchone()
        is not None
    )


def _still_there(roots, row) -> bool:
    """Whether a known source's recorded file still exists (else it moved)."""
    root = roots.get(row["root"])
    if root is None:
        return False
    try:
        return stat.S_ISREG(os.lstat(root / row["path"]).st_mode)
    except OSError:
        return False


def _reactivate(conn, source_id) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE source SET status = 'active' WHERE id = ?", (source_id,)
    )
    conn.execute("COMMIT")


def _mark_missing(conn, roots, seen) -> int:
    """Rows under a scanned root that were not seen: missing, events kept."""
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


def _process(conn, w: _Work, stats: PassStats, scopes, seen) -> None:
    """One source in one transaction; an error rolls back only it."""
    if w.row is not None:
        seen.add(w.row["id"])  # never marked missing because it failed
    try:
        conn.execute("BEGIN IMMEDIATE")
        source_id, added, removed, skipped = _write(conn, w, scopes)
        conn.execute("COMMIT")
    except Exception as exc:  # counted; the next pass redoes the source
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        stats.failed += 1
        kind = type(exc).__name__
        stats.errors[kind] = stats.errors.get(kind, 0) + 1
        return
    seen.add(source_id)
    stats.files_changed += 1
    stats.events_added += added
    stats.events_removed += removed
    stats.skipped_lines += skipped


def _write(conn, w: _Work, scopes) -> tuple[int, int, int, int]:
    info, row, provider = w.info, w.row, PROVIDER[w.name]
    parent_id, replay_mode = None, info.replay_mode
    if replay_mode == "content_prefix":
        parent_id = _source_id(conn, provider, info.forked_from_id)
        replay_mode = "content_prefix" if parent_id else "unverified"
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
    now, removed = time.time(), 0
    if row is None:
        source_id = conn.execute(
            "INSERT INTO source(session_root, parent_thread_id,"
            " forked_from_id, thread_class, class_reason, replay_mode,"
            " replay_before, root, path, first_line_sha256,"
            " classifier_version, provider, thread_id, ino, size, mtime_ns,"
            " first_seen, last_seen) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
            (*ident, provider, info.thread_id, now, now),
        ).lastrowid
    else:
        source_id = row["id"]
        conn.execute(
            "UPDATE source SET session_root=?, parent_thread_id=?,"
            " forked_from_id=?, thread_class=?, class_reason=?,"
            " replay_mode=?, replay_before=?, root=?, path=?,"
            " first_line_sha256=?, classifier_version=? WHERE id=?",
            (*ident, source_id),
        )
    resume = row is not None and w.mode == "append"
    if row is not None and not resume:
        removed = conn.execute(
            "DELETE FROM event WHERE source_id = ?", (source_id,)
        ).rowcount
        for table in ("source_issue", "usage"):
            conn.execute(
                f"DELETE FROM {table} WHERE source_id = ?", (source_id,)
            )
    if info.thread_class not in INDEXED:  # a row with stat fields only
        _commit_source(
            conn,
            source_id,
            w.st,
            now,
            (0, 0),
            (None, None),
            None,
            0,
            reset=True,
        )
        return source_id, 0, removed, 0
    start = (row["cursor_bytes"], row["cursor_line"]) if resume else (0, 0)
    anchor = (
        (row["anchor_offset"], row["anchor_sha256"])
        if resume
        else (None, None)
    )
    parsed = _parse(
        conn,
        w,
        source_id,
        provider,
        start,
        anchor,
        parent_id,
        row["parse_state"] if resume else None,
        scopes,
    )
    cursor, anchor, state, added, skipped, usage = parsed
    _commit_source(
        conn,
        source_id,
        w.st,
        now,
        cursor,
        anchor,
        state,
        skipped,
        reset=not resume,
    )
    calls, errors, last_ts = usage
    if calls or errors:
        conn.execute(
            "INSERT INTO usage(source_id, provider, session_root, calls,"
            " errors, last_ts) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET"
            " calls = calls + excluded.calls,"
            " errors = errors + excluded.errors,"
            " last_ts = CASE WHEN excluded.last_ts > coalesce(last_ts, '')"
            " THEN excluded.last_ts ELSE last_ts END",
            (source_id, provider, info.session_root, calls, errors, last_ts),
        )
    return source_id, added, removed, skipped


def _commit_source(
    conn, source_id, st, now, cursor, anchor, state, skipped, *, reset
) -> None:
    """Cursor, anchor, stat fields and parse state move with the events."""
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
            *cursor,
            *anchor,
            state,
            now,
            reset,
            skipped,
            skipped,
            source_id,
        ),
    )


def _parse(
    conn, w, source_id, provider, start, anchor, parent_id, saved, scopes
):
    """Classify and insert the new lines of one source (inside its txn)."""
    info = w.info
    state, pending, extra = _load_state(saved, info)
    events_of = (
        classify.codex_events
        if provider == "codex"
        else classify.claude_events
    )
    tombs = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT line, line_sha256 FROM tombstone WHERE provider = ?"
            " AND level = 'line' AND thread_id = ?",
            (provider, info.thread_id),
        )
    }
    prefix = _event_hashes(conn, parent_id) if extra["prefix_open"] else None
    extra["prefix_open"] = bool(prefix) and extra["prefix_open"]
    added = skipped = 0
    usage = [0, 0, None]  # pctx calls, failed pctx outputs, last call ts
    cursor = start
    with open(w.path, "rb") as handle:
        handle.seek(start[0])
        for number, begin, end, raw in _lines(handle, *start):
            cursor = (end, number)
            if raw is None:
                code = "line_too_large"
            else:
                digest = classify.record_hash(raw)
                anchor = (begin, digest)
                if (number, digest) in tombs:
                    continue  # an erased line never re-enters (I5)
                record, code = _decode(raw)
            if code:
                conn.execute(
                    "INSERT OR REPLACE INTO source_issue(source_id, line, at,"
                    " code) VALUES (?, ?, ?, ?)",
                    (source_id, number, time.time(), code),
                )
                skipped += 1
                continue
            found = events_of(record, number, state)
            if provider == "codex":
                _count_output(record, extra, usage)
            cwd = classify.cwd_of(record, state)
            for ev in found:
                if extra["prefix_open"]:
                    if _role_text(ev.role, ev.text) in prefix:
                        continue  # the fork's copy of its parent's history
                    extra["prefix_open"] = False
                event_id = _insert(
                    conn,
                    source_id,
                    begin,
                    digest,
                    ev,
                    cwd,
                    info,
                    pending,
                    scopes,
                )
                added += 1
                if ev.kind == "tool_call":
                    _note_call(ev, event_id, provider, pending, extra, usage)
    return (
        cursor,
        anchor,
        _dump_state(state, pending, extra),
        added,
        skipped,
        usage,
    )


def _lines(handle, offset, number):
    """(line number, start, end, raw) of each newline-terminated line; raw
    is None for a line over MAX_LINE_BYTES.  Stops before a partial tail:
    it is read once its newline exists (I4)."""
    while True:
        raw = handle.readline(MAX_LINE_BYTES + 1)
        if not raw.endswith(b"\n"):
            if len(raw) <= MAX_LINE_BYTES:
                return  # EOF, or a line still being written
            size = len(raw)
            while not raw.endswith(b"\n"):
                raw = handle.readline(1 << 20)
                if not raw:
                    return  # an oversize line still being written
                size += len(raw)
            number += 1
            yield number, offset, offset + size, None
            offset += size
            continue
        number += 1
        yield number, offset, offset + len(raw), raw
        offset += len(raw)


def _decode(raw: bytes) -> tuple[dict | None, str | None]:
    try:
        record = json.loads(raw)
    except RecursionError:
        return None, "too_deep"
    except ValueError:  # includes invalid UTF-8
        return None, "invalid_json"
    if not classify.within_depth(record):  # design order: depth first
        return None, "too_deep"
    if not isinstance(record, dict):
        return None, "not_object"
    return record, None


def _insert(conn, source_id, begin, digest, ev, cwd, info, pending, scopes):
    if cwd not in scopes:  # O7: a gone cwd may resolve by commit hint
        scopes[cwd] = scope.scope_id(conn, cwd or "", info.commit_hash)
    parent = pending.get(ev.call_id) if ev.kind == "tool_error" else None
    return conn.execute(
        "INSERT INTO event(source_id, line, part, byte_offset, line_sha256,"
        " seq, ts, role, kind, tag, scope_id, cwd, parent_event_id, flags,"
        " text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            ev.line,
            ev.part,
            begin,
            digest,
            ev.seq,
            ev.ts,
            ev.role,
            ev.kind,
            ev.tag,
            scopes[cwd],
            cwd,
            parent,
            ev.flags,
            ev.text,
        ),
    ).lastrowid


def _note_call(ev, event_id, provider, pending, extra, usage) -> None:
    """Remember a tool_call for linking (O1) and count pctx calls (O9)."""
    if ev.call_id:
        pending[ev.call_id] = event_id
    # ponytail: the pctx test runs on the 4 KiB-capped text; a call whose
    # pctx word sits later is not counted.
    if ev.flags & classify.FLAG_MARKER and classify.PCTX_CALL.search(ev.text):
        usage[0] += 1
        usage[2] = max(usage[2] or "", ev.ts or "") or None
        if provider == "codex" and ev.call_id:
            extra["pctx"].add(ev.call_id)


def _count_output(record, extra, usage) -> None:
    """O9: the exit status of a pctx call's output, read here, never kept.
    The output itself is skipped by classify (the call invokes pctx)."""
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return
    call_id = payload.get("call_id")
    if call_id not in extra["pctx"] or payload.get("type") not in (
        "function_call_output",
        "custom_tool_call_output",
    ):
        return
    extra["pctx"].discard(call_id)
    output = payload.get("output")
    # ponytail: an exec cell still running reports later through wait();
    # only its first output is read here.
    text = output if isinstance(output, str) else classify._join(output)
    exits = classify._EXIT.findall(text)
    if text.startswith("Script failed") or any(
        int(a or b) != 0 for a, b in exits
    ):
        usage[1] += 1


def _role_text(role: str, text: str) -> bytes:
    """sha256(role || text), the content-prefix identity (design 3.5 #3)."""
    data = f"{role}\n{text}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).digest()


def _event_hashes(conn, source_id) -> set[bytes]:
    if source_id is None:
        return set()
    rows = conn.execute(
        "SELECT role, text FROM event WHERE source_id = ?", (source_id,)
    )
    return {_role_text(role, text) for role, text in rows}


def _load_state(saved, info):
    """The classify state plus ingest's own resume fields (C2)."""
    data = json.loads(saved) if saved else {}
    state = classify.CodexState(
        cwd=data.get("cwd"),
        replay_before=info.replay_before,
        thread_class=info.thread_class,
        calls={k: tuple(v) for k, v in data.get("calls", {}).items()},
        cells={k: tuple(v) for k, v in data.get("cells", {}).items()},
    )
    extra = {
        "prefix_open": data.get("prefix_open", True),
        "pctx": set(data.get("pctx", ())),
    }
    return state, data.get("pending", {}), extra


def _dump_state(state, pending, extra) -> str:
    """JSON of what a later append pass needs; linked entries are pruned:
    a call id stays only while classify can still route an output to it."""
    live = {origin for origin, _ in state.calls.values()}
    live |= {origin for origin, _ in state.cells.values()}
    return json.dumps(
        {
            "cwd": state.cwd,
            "thread_class": state.thread_class,
            "replay_before": state.replay_before,
            "calls": state.calls,
            "cells": state.cells,
            "pending": {c: e for c, e in pending.items() if c in live},
            "prefix_open": extra["prefix_open"],
            "pctx": sorted(extra["pctx"] & set(state.calls)),
        },
        sort_keys=True,
    )
