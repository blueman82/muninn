"""Validate indexed review windows and maintain distinct source cursors."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from muninn.knowledge_models import RequestId, Scope
from muninn.obs_log import log_call
from muninn.review_activation import (
    activate_sources as activate_sources,
)
from muninn.review_activation import (
    bootstrap_policy as bootstrap_policy,
)
from muninn.review_activation import (
    boundary as boundary,
)
from muninn.review_activation import (
    checked_policy as checked_policy,
)
from muninn.review_activation import (
    digest as digest,
)
from muninn.review_activation import (
    require_transaction as require_transaction,
)
from muninn.review_evidence import (
    validate_cursor_boundary,
    validate_window_boundary,
)
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceWindow,
    OriginalRecord,
    SourceRef,
)
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_read_budget import ReadBudget
from muninn.review_selectors import check_tombstones
from muninn.review_state import baseline as setup_baseline
from muninn.review_windows import candidate_rows, resume_line


def source_row(conn: sqlite3.Connection, source: SourceRef) -> sqlite3.Row:
    """Require an active primary source with verified indexed provenance."""
    row = conn.execute(
        "SELECT * FROM source WHERE provider=? AND thread_id=?",
        (source.provider, source.thread_id),
    ).fetchone()
    if (
        row is None
        or row["status"] != "active"
        or row["thread_class"] != "primary"
        or row["replay_mode"] == "unverified"
    ):
        raise ValueError("source_unavailable")
    return row


def check_window(
    conn: sqlite3.Connection,
    window: EvidenceWindow,
    scope: Scope,
    policy: ReviewPolicy,
    *,
    budget: ReadBudget,
    selected_line: int | None = None,
) -> tuple[sqlite3.Row, OriginalRecord]:
    """Check complete boundaries, scope and erasure before admission."""
    checked_policy(policy)
    window = EvidenceWindow.model_validate_json(canonical_json(window))
    scope = Scope.model_validate_json(canonical_json(scope))
    source = source_row(conn, window.source)
    last, _last_hash = boundary(conn, source)
    if window.through_line > last:
        raise ValueError("incomplete_window")
    original = validate_window_boundary(
        conn,
        window,
        policy.max_input_bytes,
        budget=budget,
        selected_line=selected_line,
    )
    _check_interval(conn, source, window)
    scopes = conn.execute(
        "SELECT DISTINCT sc.key,sc.kind FROM event e JOIN scope sc "
        "ON sc.id=e.scope_id WHERE e.source_id=? AND e.line BETWEEN ? AND ?",
        (source["id"], window.from_line, window.through_line),
    ).fetchall()
    expected = None if scope.project_id is None else scope.project_id
    mapped = {None if r["kind"] == "global" else r["key"] for r in scopes}
    if mapped != {expected} or (expected is None and not policy.allow_global):
        raise ValueError("rejected_scope")
    return source, original


def _check_interval(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    window: EvidenceWindow,
) -> None:
    """Check indexed hashes and erased intervals without original reads."""
    locator = EvidenceLocator(
        source=window.source,
        line=window.through_line,
        part=1,
        line_sha256=window.boundary_hash,
    )
    check_tombstones(conn, source, locator)
    erased = conn.execute(
        "SELECT 1 FROM tombstone WHERE provider=? AND thread_id=? "
        "AND level='line' AND line BETWEEN ? AND ? LIMIT 1",
        (
            source["provider"],
            source["thread_id"],
            window.from_line,
            window.through_line,
        ),
    ).fetchone()
    invalid = conn.execute(
        "SELECT 1 FROM source_issue WHERE source_id=? AND line BETWEEN ? "
        "AND ? LIMIT 1",
        (source["id"], window.from_line, window.through_line),
    ).fetchone()
    if erased:
        raise ValueError("evidence_erased")
    if invalid:
        raise ValueError("incomplete_window")
    rows = conn.execute(
        "SELECT DISTINCT line,line_sha256 FROM event WHERE source_id=? "
        "AND line BETWEEN ? AND ?",
        (source["id"], window.from_line, window.through_line),
    )
    for row in rows:
        check_tombstones(
            conn,
            source,
            locator.model_copy(
                update={
                    "line": row["line"],
                    "line_sha256": row["line_sha256"],
                }
            ),
        )


def source_progress(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    now: float,
) -> sqlite3.Row:
    """Initialize an immutable activation baseline from recorded source age."""
    require_transaction(conn, now)
    activated = conn.execute(
        "SELECT activated_at FROM review_control"
    ).fetchone()[0]
    if activated is None:
        raise ValueError("review_policy_required")
    last, last_hash = boundary(conn, source)
    baseline = setup_baseline(
        conn, source, last if source["first_seen"] <= activated else 0
    )
    conn.execute(
        "INSERT OR IGNORE INTO review_source VALUES(?,?,?,?,?,?,?,?)",
        (
            source["provider"],
            source["thread_id"],
            baseline,
            baseline,
            baseline,
            baseline,
            last_hash if baseline else None,
            now,
        ),
    )
    return conn.execute(
        "SELECT * FROM review_source WHERE provider=? AND thread_id=?",
        (source["provider"], source["thread_id"]),
    ).fetchone()


def reconciliation_sources(
    conn: sqlite3.Connection,
) -> tuple[sqlite3.Row, ...]:
    """List verified primary sources without holding an active SQL cursor."""
    return tuple(
        conn.execute(
            "SELECT * FROM source WHERE status='active' "
            "AND thread_class='primary'"
            " AND replay_mode!='unverified'"
        )
    )


@contextmanager
def source_admission(
    conn: sqlite3.Connection, now: float
) -> Generator[list[RequestId]]:
    """Isolate expected source refusals without committing caller work."""
    require_transaction(conn, now)
    admitted: list[RequestId] = []
    conn.execute("SAVEPOINT review_source_admission")
    try:
        yield admitted
    except BaseException as exc:
        conn.execute("ROLLBACK TO review_source_admission")
        code = (
            {
                "source_unavailable": "unavailable",
                "original_unavailable": "unavailable",
                "ancestry_unavailable": "unavailable",
                "evidence_limit": "unavailable",
                "incomplete_window": "rejected_evidence",
                "evidence_changed": "rejected_evidence",
                "evidence_erased": "rejected_erased",
                "rejected_scope": "rejected_scope",
            }.get(str(exc))
            if isinstance(exc, ValueError)
            else None
        )
        if code is None:
            raise
        admitted.clear()
        home = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
        log_call(
            home,
            {
                "event": "review_enqueued",
                "accepted": False,
                "error_code": code,
                "recovery_gaps": 1,
            },
        )
    finally:
        conn.execute("RELEASE review_source_admission")


def reconciliation_windows(
    conn: sqlite3.Connection,
    policy: ReviewPolicy,
    now: float,
    source: sqlite3.Row,
    *,
    budget: ReadBudget,
    limit: int | None = None,
) -> tuple[tuple[EvidenceWindow, Scope], ...]:
    """Expose one source's complete new intervals by mapped scope."""
    require_transaction(conn, now)
    output: list[tuple[EvidenceWindow, Scope]] = []
    last, last_hash = boundary(conn, source)
    progress = source_progress(conn, source, now)
    if (
        last == progress["examined_line"] == progress["eligible_line"]
        and last_hash == progress["boundary_hash"]
    ):
        return ()
    if last < progress["examined_line"]:
        raise ValueError("evidence_changed")
    if progress["eligible_line"]:
        validate = (
            validate_cursor_boundary if limit else validate_window_boundary
        )
        validate(
            conn,
            EvidenceWindow(
                source=SourceRef(
                    provider=source["provider"],
                    thread_id=source["thread_id"],
                ),
                from_line=progress["eligible_line"],
                through_line=progress["eligible_line"],
                boundary_hash=progress["boundary_hash"],
            ),
            policy.max_input_bytes,
            budget=budget,
        )
    first = (
        resume_line(conn, source, progress, budget)
        if limit is not None
        else progress["eligible_line"]
    ) + 1
    rows = candidate_rows(conn, source["id"], first - 1, last, budget, limit)
    for row in rows:
        output.append(
            (
                EvidenceWindow(
                    source=SourceRef(
                        provider=source["provider"],
                        thread_id=source["thread_id"],
                    ),
                    from_line=first,
                    through_line=row["line"],
                    boundary_hash=row["line_sha256"],
                ),
                Scope(
                    project_id=(
                        None if row["kind"] == "global" else row["key"]
                    ),
                    task=None,
                ),
            )
        )
        first = row["line"] + 1
    conn.execute(
        "UPDATE review_source SET examined_line=?,updated_at=? "
        "WHERE provider=? AND thread_id=?",
        (
            rows[-1]["line"] if limit is not None and rows else last,
            now,
            source["provider"],
            source["thread_id"],
        ),
    )
    return tuple(output)


def mark_eligible(
    conn: sqlite3.Connection,
    window: EvidenceWindow,
    now: float,
) -> None:
    """Advance eligible state only after an interval is durably admitted."""
    require_transaction(conn, now)
    conn.execute(
        "UPDATE review_source SET examined_line=max(examined_line,?),"
        "boundary_hash=CASE WHEN ?>=eligible_line THEN ? "
        "ELSE boundary_hash END,"
        "eligible_line=max(eligible_line,?),updated_at=? "
        "WHERE provider=? AND thread_id=?",
        (
            window.through_line,
            window.through_line,
            window.boundary_hash,
            window.through_line,
            now,
            window.source.provider,
            window.source.thread_id,
        ),
    )


def advance_completed(
    conn: sqlite3.Connection,
    source: SourceRef,
    now: float,
) -> int:
    """Advance only through contiguous receipted successful intervals."""
    require_transaction(conn, now)
    row = conn.execute(
        "SELECT completed_line FROM review_source WHERE provider=? "
        "AND thread_id=?",
        (source.provider, source.thread_id),
    ).fetchone()
    if row is None:
        return 0
    completed = row[0]
    windows = conn.execute(
        "SELECT r.from_line,r.through_line FROM review_request r "
        "JOIN review_receipt c ON c.request_id=r.id WHERE r.provider=? "
        "AND r.thread_id=? ORDER BY r.from_line,r.through_line",
        (source.provider, source.thread_id),
    )
    for window in windows:
        if window[0] <= completed + 1:
            completed = max(completed, window[1])
    conn.execute(
        "UPDATE review_source SET completed_line=?,updated_at=? "
        "WHERE provider=? AND thread_id=?",
        (completed, now, source.provider, source.thread_id),
    )
    return completed


def unsuppressed_window(
    conn: sqlite3.Connection,
    window: EvidenceWindow,
) -> EvidenceWindow | None:
    """Exclude cancelled intervals without dropping later eligible records."""
    last = conn.execute(
        "SELECT max(through_line) FROM review_request WHERE provider=? "
        "AND thread_id=? AND state='cancelled' AND from_line<=? "
        "AND through_line>=?",
        (
            window.source.provider,
            window.source.thread_id,
            window.through_line,
            window.from_line,
        ),
    ).fetchone()[0]
    if last is None:
        return window
    if last >= window.through_line:
        return None
    return window.model_copy(update={"from_line": last + 1})
