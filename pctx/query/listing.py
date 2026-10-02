"""List sessions in scope and walk the timeline of one session."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from pctx.query.answers import (
    Answer,
    BadArgumentError,
    answer_citable,
    error,
    event_ref,
    preview,
)
from pctx.query.constants import NOTICE, PREVIEW_NOTICE
from pctx.query.filters import (
    resolve_session,
    scope_clause,
    scope_label,
    times,
)
from pctx.query.guard import guarded
from pctx.query.index_age import freshness
from pctx.scope import scope_ids_for_read

SESSIONS_MAX = 100
SESSION_PAGE_MAX = 200
FIRST_PROMPT = 120  # chars of a session's first prompt
ROW_PREVIEW = 80  # chars per line of a session timeline

# Primary threads only, so the event counts (events, kinds) leave out
# subagent and other non-primary threads; the threads and forks fields
# count every thread.
_PRIMARY_EVENTS = (
    " FROM event e JOIN source s ON s.id = e.source_id"
    " JOIN scope sc ON sc.id = e.scope_id"
    " WHERE s.thread_class = 'primary' AND "
)
# Total order of a session's events; also the keyset used for paging.
_TIMELINE_ORDER = "COALESCE(e.ts, ''), s.thread_id, e.line, e.part"


def _session_row(
    conn: sqlite3.Connection, row: sqlite3.Row, inside: str, more: list[Any]
) -> Answer:
    """Return one sessions() entry: counts, threads and forks, first prompt."""
    where = f"s.provider = ? AND s.session_root = ? AND {inside}"
    args = [row["provider"], row["session_root"], *more]
    kinds = conn.execute(
        "SELECT e.kind, count(*) AS n"
        + _PRIMARY_EVENTS
        + where
        + " GROUP BY e.kind ORDER BY e.kind",
        args,
    )
    first = conn.execute(
        "SELECT e.kind, e.flags, s.thread_class,"
        " substr(e.text, 1, 1000) AS text"
        + _PRIMARY_EVENTS
        + where
        + " AND e.kind = 'prompt' ORDER BY COALESCE(e.ts, ''), s.thread_id,"
        " e.line, e.part LIMIT 1",
        args,
    ).fetchone()
    threads = conn.execute(
        "SELECT count(*) AS n, count(forked_from_id) AS forks,"
        " sum(status = 'active') AS live FROM source"
        " WHERE provider = ? AND session_root = ?",
        args[:2],
    ).fetchone()
    return {
        "session": row["session_root"],
        "provider": row["provider"],
        "first_ts": row["first_ts"],
        "last_ts": row["last_ts"],
        "events": row["events"],
        "kinds": {k["kind"]: k["n"] for k in kinds},
        "threads": threads["n"],
        "forks": threads["forks"],
        "preview": preview(first["text"])[:FIRST_PROMPT] if first else None,
        "preview_flagged": bool(first and first["flags"] & 1),
        "preview_answer_citable": bool(first and answer_citable(first)),
        "status": "active" if threads["live"] else "missing",
        "scope": row["label"],
    }


@guarded
def sessions(
    conn: sqlite3.Connection,
    *,
    cwd: str,
    all_projects: bool = False,
    since: str | None = None,
    limit: int = 20,
    status: Mapping[str, Any] | None = None,
) -> Answer:
    """Return the sessions in scope, newest first.

    Counts, first/last ts and the first prompt cover the session's primary
    threads; ``threads`` and ``forks`` count every thread pctx has
    classified.

    Args:
        conn: Read-only store connection.
        cwd: Directory that decides the scope.
        all_projects: Drop the scope filter.
        since: Keep sessions whose last event is at or after this date.
        limit: Most sessions to return, capped at SESSIONS_MAX.
        status: A parsed status.json, for freshness fields.

    Returns:
        The answer, or ``{"error": code}`` for a bad date.
    """
    limit = min(max(limit, 1), SESSIONS_MAX)
    try:
        after, after_params = times(since, None)
    except BadArgumentError as bad:
        return error(str(bad))
    ids = scope_ids_for_read(conn, cwd)
    inside, more = scope_clause(ids, None, all_projects)
    having = "HAVING max(e.ts) >= ?" if after else ""
    # One extra row tells has_more without a second count query.
    rows = conn.execute(
        "SELECT s.provider, s.session_root, min(e.ts) AS first_ts,"
        " max(e.ts) AS last_ts, count(*) AS events, min(sc.label) AS label"
        + _PRIMARY_EVENTS
        + inside
        + f" GROUP BY s.provider, s.session_root {having}"
        " ORDER BY max(e.ts) DESC, s.session_root LIMIT ?",
        [*more, *after_params, limit + 1],
    ).fetchall()
    return {
        "notice": NOTICE,
        "preview_notice": PREVIEW_NOTICE,
        "scope": scope_label(conn, ids, None, all_projects),
        "sessions": [
            _session_row(conn, r, inside, more) for r in rows[:limit]
        ],
        "has_more": len(rows) > limit,
        **freshness(status),
    }


def _timeline_event(row: sqlite3.Row) -> Answer:
    """Return one timeline line for an event row."""
    event: Answer = {
        "id": row["id"],
        "ref": event_ref(row),
        "ts": row["ts"],
        "role": row["role"],
        "kind": row["kind"],
        "tag": row["tag"],
        "preview": preview(row["text"])[:ROW_PREVIEW],
        "answer_citable": answer_citable(row),
    }
    if row["thread_class"] != "primary":
        event["class"] = row["thread_class"]
    if row["flags"] & 1:
        event["flagged"] = True
    return event


def _keyset(
    conn: sqlite3.Connection, root: str, from_id: int
) -> tuple[str, list[Any]] | None:
    """Return the clause and parameters that resume at event ``from_id``.

    Args:
        conn: Read-only store connection.
        root: The session root the event must belong to.
        from_id: Id of the event to resume at.

    Returns:
        The SQL clause and its parameters that keep events at or after the
        event's (ts, thread, line, part) tuple, or None when the event is
        not part of this session.
    """
    start = conn.execute(
        f"SELECT {_TIMELINE_ORDER} FROM event e"
        " JOIN source s ON s.id = e.source_id"
        " WHERE e.id = ? AND s.session_root = ?",
        (from_id, root),
    ).fetchone()
    if start is None:
        return None
    return f" AND ({_TIMELINE_ORDER}) >= (?, ?, ?, ?)", list(start)


@guarded
def session(
    conn: sqlite3.Connection,
    root: str,
    *,
    from_id: int | None = None,
    limit: int = 50,
    status: Mapping[str, Any] | None = None,
) -> Answer:
    """Return every event of every thread of one session, one line each.

    Ordered by ts, then thread, line and part.

    Args:
        conn: Read-only store connection.
        root: The session root, or an unambiguous prefix of it.
        from_id: Event id to resume at; pass the previous ``next_from``.
        limit: Most events to return, capped at SESSION_PAGE_MAX.
        status: A parsed status.json, for freshness fields.

    Returns:
        The answer, or ``{"error": code}`` for an unknown or ambiguous
        session or a ``from_id`` outside it.
    """
    limit = min(max(limit, 1), SESSION_PAGE_MAX)
    try:
        root = resolve_session(conn, root)
    except BadArgumentError as bad:
        return error(str(bad))
    where, args = "s.session_root = ?", [root]
    if from_id is not None:
        resume = _keyset(conn, root, from_id)
        if resume is None:
            return error("bad_from")
        where += resume[0]
        args += resume[1]
    # One extra row tells whether there is a next page and where it starts.
    rows = conn.execute(
        "SELECT e.id, e.line, e.part, e.ts, e.role, e.kind, e.tag, e.flags,"
        " substr(e.text, 1, 500) AS text, s.provider, s.thread_id,"
        " s.thread_class FROM event e JOIN source s ON s.id = e.source_id"
        f" WHERE {where} ORDER BY {_TIMELINE_ORDER} LIMIT ?",
        [*args, limit + 1],
    ).fetchall()
    total = conn.execute(
        "SELECT count(*) FROM event e JOIN source s ON s.id = e.source_id"
        " WHERE s.session_root = ?",
        (root,),
    ).fetchone()[0]
    return {
        "notice": NOTICE,
        "preview_notice": PREVIEW_NOTICE,
        "session": root,
        "provider": conn.execute(
            "SELECT provider FROM source WHERE session_root = ? LIMIT 1",
            (root,),
        ).fetchone()[0],
        "total": total,
        "events": [_timeline_event(r) for r in rows[:limit]],
        "next_from": rows[limit]["id"] if len(rows) > limit else None,
        **freshness(status),
    }
