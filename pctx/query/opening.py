"""Open one event in full: text, provenance, neighbours and raw line."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pctx.query.answers import (
    Answer,
    answer_citable,
    error,
    event_ref,
    preview,
)
from pctx.query.constants import NOTICE, PREVIEW_NOTICE
from pctx.query.freshness import freshness
from pctx.query.guard import guarded
from pctx.query.terms import parse_ref

OPEN_BYTES = 12_000  # text bytes per open page
CONTEXT_MAX = 20
RAW_LIMIT = 64 * 1024  # longest raw line open will return
LINE_CAP = 8 * 1024 * 1024  # ingest skips longer lines

_EVENT_SQL = (
    "SELECT e.*, s.provider, s.thread_id, s.session_root, s.thread_class,"
    " s.root, s.path, s.status, sc.label FROM event e"
    " JOIN source s ON s.id = e.source_id JOIN scope sc ON sc.id = e.scope_id"
    " WHERE "
)


def _by_ref(conn: sqlite3.Connection, text: str) -> list[sqlite3.Row] | None:
    """Look an event up by REF; None when the text is not a REF."""
    try:
        provider, thread, line, part = parse_ref(text)
    except ValueError:
        return None
    rows: list[sqlite3.Row] = []
    # An exact thread id beats a prefix match, so a prefix that happens to
    # equal another thread's full id never makes the REF ambiguous.
    for match, args in (
        ("s.thread_id = ?", (thread,)),
        ("substr(s.thread_id, 1, ?) = ?", (len(thread), thread)),
    ):
        rows = conn.execute(
            _EVENT_SQL + f"s.provider = ? AND {match} AND e.line = ?"
            " AND e.part = ? LIMIT 2",
            (provider, *args, line, part),
        ).fetchall()
        if rows:
            break
    return rows


def locate(
    conn: sqlite3.Connection, event: str | int
) -> tuple[sqlite3.Row | None, str]:
    """Find an event by id or REF.

    Args:
        conn: Read-only store connection.
        event: An event id or a REF (thread prefix allowed).

    Returns:
        The event row and an empty string, or None and an error code
        (bad_ref, ambiguous_ref, not_found).
    """
    text = str(event).strip()
    if re.fullmatch(r"[0-9]{1,18}", text):
        rows = conn.execute(_EVENT_SQL + "e.id = ?", (int(text),)).fetchall()
    else:
        found = _by_ref(conn, text)
        if found is None:
            return None, "bad_ref"
        rows = found
    if len(rows) > 1:
        return None, "ambiguous_ref"
    return (rows[0], "") if rows else (None, "not_found")


def _read_line(roots: Mapping[str, Path], row: sqlite3.Row) -> bytes | None:
    """Read the line at byte_offset, terminator included.

    Only an active source under a known root is read, never through a
    symlink or out of the root, and never blocking on a non-regular file
    (a FIFO planted at the path would otherwise hang the reader).

    Args:
        roots: Root name to directory.
        row: An event row joined with its source.

    Returns:
        The line bytes, or None when the line cannot be read safely.
    """
    root = (roots or {}).get(row["root"])
    if root is None or row["status"] != "active":
        return None
    base = os.path.normpath(root)
    path = os.path.normpath(Path(base) / row["path"])
    try:
        if os.path.commonpath([base, path]) != base:
            return None
        # O_NOFOLLOW refuses a symlinked final component; O_NONBLOCK lets
        # open() return at once on a FIFO so the S_ISREG check can reject it.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        handle.seek(row["byte_offset"])
        # One byte over the cap so an over-long line is seen as such.
        return handle.readline(LINE_CAP + 1)


def _neighbours(
    conn: sqlite3.Connection, row: sqlite3.Row, n: int
) -> list[Answer]:
    """Return the n events before and after in (line, part) order."""
    found: list[Answer] = []
    for sign, cmp, order in ((-1, "<", "DESC"), (1, ">", "ASC")):
        rows = conn.execute(
            "SELECT e.id, e.ts, e.role, e.kind, e.tag, e.flags,"
            " s.thread_class, substr(e.text, 1, 1000) AS text"
            " FROM event e JOIN source s ON s.id = e.source_id"
            f" WHERE source_id = ? AND (line, part) {cmp} (?, ?)"
            f" ORDER BY line {order}, part {order} LIMIT ?",
            (row["source_id"], row["line"], row["part"], n),
        )
        found += [
            {
                "id": r["id"],
                "rel": sign * rank,
                "ts": r["ts"],
                "role": r["role"],
                "kind": r["kind"],
                "tag": r["tag"],
                "preview": preview(r["text"]),
                "flagged": bool(r["flags"] & 1),
                "answer_citable": answer_citable(r),
            }
            for rank, r in enumerate(rows, 1)
        ]
    return sorted(found, key=lambda n: n["rel"])


def _raw(row: sqlite3.Row, body: bytes | None, hash_ok: bool | None) -> Answer:
    """Return the raw-line part of an answer: ``raw``, or why none."""
    if row["flags"] & 2:  # the line still holds the secret
        return {"raw_redacted": True}
    if body is None:
        return {"error": "raw_unavailable"}
    if len(body) > RAW_LIMIT:
        return {"error": "line_too_large"}
    if not hash_ok:
        return {"error": "raw_hash_mismatch"}
    try:
        return {"raw": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {"error": "raw_not_utf8"}


def _provenance(row: sqlite3.Row) -> dict[str, Any]:
    """Return where an event came from and how to find it again."""
    provenance: dict[str, Any] = {
        "provider": row["provider"],
        "thread": row["thread_id"],
        "session": row["session_root"],
        "ts": row["ts"],
        "role": row["role"],
        "kind": row["kind"],
        "tag": row["tag"],
        "scope": row["label"],
        "cwd": row["cwd"],
        "root": row["root"],
        "path": row["path"],
        "line": row["line"],
        "part": row["part"],
        "byte_offset": row["byte_offset"],
        "line_sha256": row["line_sha256"],
        "source_status": row["status"],
    }
    if row["thread_class"] != "primary":
        provenance["class"] = row["thread_class"]
    if row["parent_event_id"] is not None:
        provenance["parent_event_id"] = row["parent_event_id"]
    return provenance


def _page_of(text: str, offset: int) -> str:
    """Return the text page at a character offset, within OPEN_BYTES."""
    page = text[offset : offset + OPEN_BYTES].encode()[:OPEN_BYTES]
    # Cutting at a byte count can split a character; drop the fragment.
    return page.decode("utf-8", "ignore")


@guarded
def open_event(
    conn: sqlite3.Connection,
    ref: str,
    *,
    roots: Mapping[str, Path],
    context: int = 3,
    offset: int = 0,
    raw: bool = False,
    status: Mapping[str, Any] | None = None,
) -> Answer:
    """Return one event in full: text, provenance and neighbours.

    The text is served in pages of at most OPEN_BYTES of UTF-8 from a
    character offset; ``next_offset`` continues it. ``hash_ok`` re-reads the
    line at byte_offset under ``roots`` while the source is active (None
    when it cannot be checked). ``raw`` adds that verified line as ``raw``,
    unless the event is redacted (``raw_redacted``), the line is over
    RAW_LIMIT (error ``line_too_large``) or cannot be verified.

    Args:
        conn: Read-only store connection.
        ref: An event id or ``provider:thread_id:line.part`` (a thread
            prefix is accepted).
        roots: Root name to directory, used to verify the source line.
        context: Neighbouring events to show on each side, capped.
        offset: Character offset into the event text.
        raw: Include the verified raw source line.
        status: A parsed status.json, for freshness fields.

    Returns:
        The answer, or ``{"error": code}`` for a bad or unknown reference.
    """
    row, problem = locate(conn, ref)
    if row is None:
        return error(problem)
    text = row["text"]
    if offset < 0:
        return error("bad_offset")
    if offset > len(text):
        return error("offset_past_end")
    page = _page_of(text, offset)
    line = _read_line(roots, row)
    body = None if line is None else line.rstrip(b"\r\n")
    hash_ok = None
    if body is not None:
        hash_ok = hashlib.sha256(body).hexdigest() == row["line_sha256"]
    end = offset + len(page)
    out: Answer = {
        "notice": NOTICE,
        "preview_notice": PREVIEW_NOTICE,
        "id": row["id"],
        "ref": event_ref(row),
        "provenance": _provenance(row),
        "answer_citable": answer_citable(row),
        "flagged": bool(row["flags"] & 1),
        "redacted": bool(row["flags"] & 2),
        "truncated": bool(row["flags"] & 4),
        "text": page,
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "chars": len(text),
        "hash_ok": hash_ok,
        "neighbours": _neighbours(
            conn, row, min(max(context, 0), CONTEXT_MAX)
        ),
    }
    if raw:
        out |= _raw(row, body, hash_ok)
    return out | freshness(status)
