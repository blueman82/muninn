"""Select bounded candidate boundaries without consuming ordinary tails."""

from __future__ import annotations

import sqlite3

from muninn.review_read_budget import ReadBudget


def resume_line(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    progress: sqlite3.Row,
    budget: ReadBudget,
) -> int:
    """Retain refused endpoints while carrying ordinary tails forward."""
    eligible, examined = progress["eligible_line"], progress["examined_line"]
    if examined <= eligible:
        return eligible
    # Returned candidates are admitted atomically or recorded as refused.
    # An observation beyond those endpoints must not discard tool/noop tails.
    conn.set_progress_handler(budget.sqlite_progress, 1)
    try:
        line = conn.execute(
            "SELECT max(line) FROM (SELECT line FROM event "
            "WHERE source_id=? AND line>? AND line<=? "
            "AND kind IN ('prompt','reply') UNION ALL "
            "SELECT line FROM tombstone WHERE provider=? AND thread_id=? "
            "AND level='line' AND line>? AND line<=?)",
            (
                source["id"],
                eligible,
                examined,
                source["provider"],
                source["thread_id"],
                eligible,
                examined,
            ),
        ).fetchone()[0]
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise ValueError("evidence_limit") from None
        raise
    finally:
        conn.set_progress_handler(None, 0)
    return max(eligible, line or 0)


def candidate_rows(
    conn: sqlite3.Connection,
    source_id: int,
    first: int,
    last: int,
    budget: ReadBudget,
    limit: int | None,
) -> list[sqlite3.Row]:
    """Bound the candidate SELECT before any further progress write."""
    if limit not in (None, 1):
        raise ValueError("invalid_window_limit")
    ordering = (
        "GROUP BY e.line,sc.key ORDER BY e.line"
        if limit is None
        else "ORDER BY e.line,e.part"
    )
    if limit is not None:
        conn.set_progress_handler(budget.sqlite_progress, 1)
    try:
        return conn.execute(
            "SELECT e.line,e.line_sha256,sc.key,sc.kind FROM event e "
            "JOIN scope sc ON sc.id=e.scope_id WHERE e.source_id=? "
            "AND e.line>? AND e.line<=? AND e.kind IN ('prompt','reply') "
            f"{ordering} LIMIT ?",
            (source_id, first, last, -1 if limit is None else limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if limit is not None and getattr(exc, "sqlite_errorcode", None) == (
            sqlite3.SQLITE_INTERRUPT
        ):
            raise ValueError("evidence_limit") from None
        raise
    finally:
        if limit is not None:
            conn.set_progress_handler(None, 0)
