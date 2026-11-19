"""Cut or skip a live or catch-up window that does not fit the read limit.

ADR 0025: each window gets its own read budget for its admission check. A
JSONL window keeps its last line and loses leading lines until it holds at
most `MAX_LINES` lines and at most the safe limit of original bytes. Muninn
skips a window when its last line alone passes the safe limit, or when its
admission check still meets a read limit. Each cut or skip writes one
content-free `review_skipped` log record, and the skipped lines stay
examined.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from muninn.knowledge_models import Scope
from muninn.obs_log import log_call
from muninn.review_evidence_models import EvidenceWindow
from muninn.review_models import ReviewPolicy
from muninn.review_progress import mark_eligible, unsuppressed_window
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from muninn.review_request_views import RequestView
from muninn.review_requests import enqueue_review
from muninn.review_split import MAX_LINES, safe_limit


def _start(
    conn: sqlite3.Connection, source: sqlite3.Row, line: int, end: int
) -> int:
    """Find the indexed byte offset at which a line is charged to begin.

    A line with no indexed event is charged from the next indexed line, as
    `review_history_plan` does. Line 1 always begins at offset 0.

    Args:
        conn: The store connection.
        source: The source row.
        line: The physical line number.
        end: The offset to give when no later line is indexed.

    Returns:
        The byte offset.
    """
    if line == 1:
        return 0
    row = conn.execute(
        "SELECT byte_offset FROM event WHERE source_id=? AND line>=? "
        "ORDER BY line,part LIMIT 1",
        (source["id"], line),
    ).fetchone()
    return end if row is None else int(row[0])


def fitted_start(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    window: EvidenceWindow,
    max_input_bytes: int,
) -> tuple[int | None, str]:
    """Pick the first line of the part of a window that fits the bounds.

    Cursor windows are not cut (ADR 0023 keeps the Cursor reader).

    Args:
        conn: The store connection.
        source: The source row of the window.
        window: The planned window.
        max_input_bytes: The policy read limit.

    Returns:
        The new first line, or None to skip the window, and the limit code
        of a cut or skip.
    """
    if source["provider"] == "cursor":
        return window.from_line, "evidence_limit"
    safe = safe_limit(max_input_bytes)
    through = window.through_line
    end = _start(conn, source, through + 1, source["cursor_bytes"])
    if end - _start(conn, source, through, end) > safe:
        return None, "evidence_byte_limit"
    code = (
        "evidence_byte_limit"
        if end - _start(conn, source, window.from_line, end) > safe
        else "evidence_record_limit"
    )
    first = max(window.from_line, through - MAX_LINES + 1)
    while end - _start(conn, source, first, end) > safe:
        first += 1
    return first, code


def _log_skip(conn: sqlite3.Connection, code: str, count: int) -> None:
    """Write one content-free skip record to the call log.

    Args:
        conn: The store connection, which names the data folder.
        code: The read-limit code of the cut or skip.
        count: The number of skipped lines.
    """
    home = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    log_call(
        home,
        {
            "event": "review_skipped",
            "error_code": code,
            "skipped_lines": count,
        },
    )


def _skip(
    conn: sqlite3.Connection, window: EvidenceWindow, code: str, now: float
) -> None:
    """Keep all lines of a skipped window examined and log the skip.

    Args:
        conn: The store connection, in the caller's transaction.
        window: The skipped window.
        code: The read-limit code of the skip.
        now: The time of the change.
    """
    conn.execute(
        "UPDATE review_source SET examined_line=max(examined_line,?),"
        "updated_at=? WHERE provider=? AND thread_id=?",
        (
            window.through_line,
            now,
            window.source.provider,
            window.source.thread_id,
        ),
    )
    _log_skip(conn, code, window.through_line - window.from_line + 1)


def admit_window(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    window: EvidenceWindow,
    scope: Scope,
    policy: ReviewPolicy,
    trigger: str,
    now: float,
) -> RequestView | None:
    """Admit one window with its own budget, or cut or skip it.

    Args:
        conn: The store connection, in the caller's transaction.
        source: The source row of the window.
        window: The planned window.
        scope: The mapped scope of the window.
        policy: The installed review policy.
        trigger: The admission trigger, `hook` or `reconcile`.
        now: The time of the admission.

    Returns:
        The request, or None when the window is suppressed or skipped.

    Raises:
        ValueError: If the admission check refuses the window for a cause
            other than a read limit.
    """
    permitted = unsuppressed_window(conn, window)
    if permitted is None:
        mark_eligible(conn, window, now)
        return None
    first, code = fitted_start(conn, source, permitted, policy.max_input_bytes)
    if first is None:
        _skip(conn, permitted, code, now)
        return None
    fitted = permitted.model_copy(update={"from_line": first})
    conn.execute("SAVEPOINT review_window_fit")
    try:
        request = enqueue_review(
            conn,
            fitted,
            scope,
            policy,
            trigger,
            now,
            budget=ReadBudget(policy.max_input_bytes),
        )
    except ValueError as exc:
        conn.execute("ROLLBACK TO review_window_fit")
        if str(exc) != "evidence_limit":
            raise
        limit = exc.code if isinstance(exc, EvidenceLimitError) else None
        _skip(conn, permitted, limit or "evidence_limit", now)
        return None
    finally:
        conn.execute("RELEASE review_window_fit")
    if first > permitted.from_line:
        _log_skip(conn, code, first - permitted.from_line)
    return request
