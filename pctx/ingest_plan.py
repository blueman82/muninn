"""Ingest planning: find transcript files and decide what each one needs.

Planning reads at most line 1 of a file and never writes a source row, with
one exception: a file that came back after being marked missing is
reactivated in its own small transaction.  The expensive work (parsing and
inserting events) is left to `pctx.ingest`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path

from pctx import classify, ingest_model
from pctx.ingest_model import (
    INDEXED,
    PATTERN,
    PROVIDER,
    PassStats,
    PlanOptions,
    Work,
)

__all__ = ["discover", "late_forks", "plan_source", "source_id_for"]

# The rollout uuid ending a Codex file name.  It equals line-1 payload.id,
# except in a long thread's continuation files, which keep payload.id and
# get a new uuid (and history_base); each file is one transcript.
_NAME_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def discover(
    roots: Mapping[str, Path],
) -> Iterator[tuple[str, Path, os.stat_result]]:
    """Yield (root name, path, lstat) of each regular file, in path order.

    Symlinks, FIFOs and directories are never opened: a FIFO would block
    the pass, and rglob does not follow symlinked dirs, so a link cannot
    lead the scan outside the root.

    Args:
        roots: Provider root name to directory.

    Yields:
        One tuple per candidate transcript file.

    Raises:
        ValueError: If a root name is not a known provider root.
    """
    for name, root in roots.items():
        if name not in PROVIDER:
            raise ValueError(f"unknown root {name!r}")
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(PATTERN[name])):
            try:
                st = path.lstat()
            except OSError:
                continue  # vanished between listing and stat
            if not stat.S_ISREG(st.st_mode):
                continue
            yield name, path, st


def source_id_for(
    conn: sqlite3.Connection, provider: str, thread_id: str | None
) -> int | None:
    """Return the source row id of a thread, or None when not indexed."""
    if not thread_id:
        return None
    row = conn.execute(
        "SELECT id FROM source WHERE provider = ? AND thread_id = ?",
        (provider, thread_id),
    ).fetchone()
    return row[0] if row else None


def late_forks(
    conn: sqlite3.Connection, opts: PlanOptions, stats: PassStats
) -> Iterator[Work]:
    """Yield unchanged 'unverified' old forks whose parent arrived this pass.

    The first planning round skips such a fork as unchanged, because its
    parent was not indexed yet; once the parent's source is written the
    content-prefix rule can finally be applied, so the fork is planned again
    as if forced.
    """
    rows = conn.execute(
        "SELECT root, path FROM source WHERE replay_mode = 'unverified'"
    ).fetchall()
    again = dataclasses.replace(opts, full=False)
    for row in rows:
        if row["root"] not in opts.roots:
            continue
        path = opts.roots[row["root"]] / row["path"]
        try:
            st = path.lstat()
        except OSError:
            continue
        item = plan_source(conn, again, stats, row["root"], path, st)
        if isinstance(item, Work):
            yield item


def plan_source(
    conn: sqlite3.Connection,
    opts: PlanOptions,
    stats: PassStats,
    name: str,
    path: Path,
    st: os.stat_result,
) -> Work | int | None:
    """Plan one file, skipping it if it vanished or became unreadable.

    An archive move can race the pass; the next pass sees the new state, so
    the file is only counted as skipped.

    Args:
        conn: Open store connection.
        opts: Roots and flags of this pass.
        stats: Counters; skipped files are added here.
        name: Provider root name the file was found under.
        path: The file.
        st: The lstat taken when the file was listed.

    Returns:
        A Work item to write, the id of a known source to count as seen, or
        None when the file is skipped and nothing is known about it.
    """
    try:
        return _plan(conn, opts, stats, name, path, st)
    except OSError:
        stats.skipped_files += 1
        return None


def _plan(
    conn: sqlite3.Connection,
    opts: PlanOptions,
    stats: PassStats,
    name: str,
    path: Path,
    st: os.stat_result,
) -> Work | int | None:
    """Decide what one file needs; see plan_source for the result."""
    rel = path.relative_to(opts.roots[name]).as_posix()
    row = conn.execute(
        "SELECT * FROM source WHERE root = ? AND path = ?", (name, rel)
    ).fetchone()
    if row is not None and not opts.full and _unchanged(conn, row, st):
        if row["status"] == "missing":  # back, byte-identical
            _reactivate(conn, row["id"])
        # The cheap path: an unchanged file is never opened, which keeps a
        # pass over a large, mostly idle archive fast.
        return row["id"]
    first = _first_line(path)
    info, base = _identify(name, opts.roots[name], path, first)
    if info is None or first is None:
        # Line 1 incomplete, oversize or not a thread header.
        stats.skipped_files += 1
        return row["id"] if row is not None else None
    row, seen_id, proceed = _vet(conn, opts, stats, name, row, info, base)
    if not proceed:
        return seen_id
    mode = _mode(conn, row, info, first, st, path, opts.full)
    return Work(name, path, rel, st, info, row, first, mode)


def _vet(
    conn: sqlite3.Connection,
    opts: PlanOptions,
    stats: PassStats,
    name: str,
    row: sqlite3.Row | None,
    info: classify.ThreadInfo,
    base: str | None,
) -> tuple[sqlite3.Row | None, int | None, bool]:
    """Decide whether an identified file may be indexed.

    Returns:
        (source row to update or None, source id to count as seen, whether
        to proceed).  The row differs from the argument when a new path
        turns out to be a known thread that moved.
    """
    if opts.only_threads and not opts.only_threads & {
        info.thread_id,
        base,
        info.session_root,
    }:
        return row, None, False
    provider = PROVIDER[name]
    if _tombstoned(conn, provider, info, base):  # before any line is read
        stats.skipped_files += 1
        return row, None, False
    if row is None:
        row = conn.execute(
            "SELECT * FROM source WHERE provider = ? AND thread_id = ?",
            (provider, info.thread_id),
        ).fetchone()
        if row is not None and _still_there(opts.roots, row):
            # A copy is not indexed twice.
            stats.skipped_files += 1
            return row, None, False
        # Otherwise a rename or archive move: the write updates root and
        # path on the existing row.
    elif row["thread_id"] != info.thread_id:
        stats.skipped_files += 1  # another thread under a known name
        return row, row["id"], False
    return row, None, True


def _unchanged(
    conn: sqlite3.Connection, row: sqlite3.Row, st: os.stat_result
) -> bool:
    """Whether stat and classifier version match the row and nothing waits."""
    return (
        (row["ino"], row["size"], row["mtime_ns"])
        == (st.st_ino, st.st_size, st.st_mtime_ns)
        and row["classifier_version"] == classify.CLASSIFIER_VERSION
        and not _parent_arrived(conn, row)
    )


def _parent_arrived(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Whether an 'unverified' old fork's parent is now indexed."""
    return row["replay_mode"] == "unverified" and bool(
        source_id_for(conn, row["provider"], row["forked_from_id"])
    )


def _mode(
    conn: sqlite3.Connection,
    row: sqlite3.Row | None,
    info: classify.ThreadInfo,
    first: bytes,
    st: os.stat_result,
    path: Path,
    full: bool,
) -> str:
    """Return ``new``, ``append`` or ``replace`` for a file to be written.

    Append is only safe when everything already committed is provably
    untouched: same classifier, same first line, no truncation, and the
    last committed line (the anchor) still hashes the same.  Anything else
    rebuilds the source from scratch.
    """
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
    """Return line 1 with its newline, or None while incomplete or oversize."""
    with path.open("rb") as handle:
        raw = handle.readline(ingest_model.MAX_LINE_BYTES + 1)
    return raw if raw.endswith(b"\n") else None


def _hash_at(path: Path, offset: int) -> str | None:
    """Return the hash of the line starting at ``offset``, if complete."""
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.readline(ingest_model.MAX_LINE_BYTES + 1)
    return classify.record_hash(raw) if raw.endswith(b"\n") else None


def _identify(
    name: str, root: Path, path: Path, first: bytes | None
) -> tuple[classify.ThreadInfo | None, str | None]:
    """Read a file's thread identity from its first line.

    Returns:
        (ThreadInfo, base thread id of a continuation file or None), or
        (None, None) when line 1 is not a usable thread header.
    """
    try:
        record = json.loads(first) if first else None
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(record, dict):
        return None, None
    if PROVIDER[name] == "claude":
        rel = path.relative_to(root).as_posix()
        return classify.claude_thread(rel, record), None
    try:
        info = classify.codex_thread(record)
    except ValueError:  # not a session_meta with an id
        return None, None
    own = _NAME_ID.search(path.stem)
    if own is None or own.group() == info.thread_id:
        return info, None
    # A continuation file: keep the shared session identity but index it as
    # its own thread (its file-name uuid), parented to the original.
    segment = dataclasses.replace(
        info,
        thread_id=own.group(),
        parent_thread_id=info.parent_thread_id or info.thread_id,
    )
    return segment, info.thread_id


def _tombstoned(
    conn: sqlite3.Connection,
    provider: str,
    info: classify.ThreadInfo,
    base: str | None = None,
) -> bool:
    """Whether erasure forbids indexing this file.

    A thread tombstone on this file's thread or the thread it continues, or
    a session tombstone on its session or the one it was forked from, blocks
    it.  Checked before any line is read so erased text never re-enters.

    Returns:
        True if the file must not be indexed.
    """
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
            "(level = 'thread' AND thread_id IN (?, ?)) OR "
            f"(level = 'session' AND session_root IN ({marks}))) LIMIT 1",
            (provider, info.thread_id, base or info.thread_id, *sessions),
        ).fetchone()
        is not None
    )


def _still_there(roots: Mapping[str, Path], row: sqlite3.Row) -> bool:
    """Whether a known source's recorded file still exists (else it moved)."""
    root = roots.get(row["root"])
    if root is None:
        return False
    try:
        return stat.S_ISREG((root / row["path"]).lstat().st_mode)
    except OSError:
        return False


def _reactivate(conn: sqlite3.Connection, source_id: int) -> None:
    """Mark a source active again, in its own transaction."""
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE source SET status = 'active' WHERE id = ?", (source_id,)
    )
    conn.execute("COMMIT")
