"""Scan verified transcript history and apply an explicit learning scope."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from typing import Literal

from muninn import classify, review_state
from muninn.knowledge_models import Scope, StrictModel
from muninn.review_estimate import estimate
from muninn.review_evidence_models import EvidenceWindow, SourceRef
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_originals import originals
from muninn.review_progress import (
    boundary,
    check_window,
    checked_policy,
    digest,
    require_transaction,
)
from muninn.review_read_budget import ReadBudget
from muninn.review_requests import enqueue_review

_EXCLUDED_CAUSES = {
    "source_unavailable": "The source is not a verified primary transcript.",
    "original_unavailable": "The original transcript is unavailable.",
    "evidence_changed": "Original bytes differ from the indexed record hash.",
    "evidence_erased": "An erase record excludes this source range.",
    "incomplete_window": "The range is incomplete or has indexing errors.",
    "evidence_limit": "Source verification exceeded the policy read limit.",
    "rejected_scope": "The range is outside the selected project.",
    "ancestry_unavailable": "Muninn cannot verify the source history.",
    "source_unverifiable": "Muninn could not verify this source range.",
}


class HistoryItem(StrictModel):
    """Carry an exact verified source interval and its project scope."""

    window: EvidenceWindow
    scope: Scope


class HistoryScan(StrictModel):
    """Present verified work, exclusions and explicitly uncertain estimates."""

    policy_digest: str
    project_id: str | None
    items: list[HistoryItem]
    excluded: list[dict[str, str | int]]
    estimate: dict[str, str | int | float | None]

    @property
    def digest(self) -> str:
        """Bind confirmation to the complete checked scan result."""
        return hashlib.sha256(canonical_json(self).encode()).hexdigest()


def _covered(
    conn: sqlite3.Connection, source: sqlite3.Row, policy: ReviewPolicy
) -> list[tuple[int, int]]:
    """Keep completed, cancelled, active and exhausted work out of replay."""
    rows = conn.execute(
        "SELECT r.from_line,r.through_line FROM review_request r "
        "WHERE r.provider=? AND r.thread_id=? AND ("
        "r.state IN ('completed','running','pending','waiting') "
        "OR (r.state='cancelled' AND "
        "coalesce(r.cancel_code,'')!='policy_replaced') "
        "OR (r.state='failed' AND (r.policy_digest=? OR "
        "NOT EXISTS(SELECT 1 FROM review_attempt a WHERE a.request_id=r.id "
        "AND a.error_code IN "
        "('config','unavailable','deadline','rejected_revision')) "
        "OR EXISTS(SELECT 1 FROM review_call c JOIN review_attempt a "
        "ON a.id=c.attempt_id "
        "WHERE a.request_id=r.id AND c.state!='completed'))))",
        (source["provider"], source["thread_id"], digest(policy)),
    )
    return [(row[0], row[1]) for row in rows]


def _windows(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    policy: ReviewPolicy,
    project: str | None,
) -> list[HistoryItem]:
    """Group same-scope indexed lines into intervals of at most 200 lines."""
    rows = conn.execute(
        "SELECT e.line,e.line_sha256,sc.key,sc.kind,max(e.kind IN "
        "('prompt','reply')) AS candidate "
        "FROM event e JOIN scope sc ON sc.id=e.scope_id WHERE e.source_id=? "
        "GROUP BY e.line,sc.id ORDER BY e.line",
        (source["id"],),
    ).fetchall()
    lines: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        lines[row["line"]].append(row)
    covered = _covered(conn, source, policy)
    output: list[HistoryItem] = []
    group: list[sqlite3.Row] = []

    def flush() -> None:
        if group and any(row["candidate"] for row in group):
            first, last = group[0], group[-1]
            output.append(
                HistoryItem(
                    window=EvidenceWindow(
                        source=SourceRef(
                            provider=source["provider"],
                            thread_id=source["thread_id"],
                        ),
                        from_line=first["line"],
                        through_line=last["line"],
                        boundary_hash=last["line_sha256"],
                    ),
                    scope=Scope(project_id=first["key"], task=None),
                )
            )
        group.clear()

    for line, parts in lines.items():
        row = parts[0]
        if (
            len(parts) != 1
            or row["kind"] == "global"
            or (project is not None and row["key"] != project)
            or any(a <= line <= b for a, b in covered)
        ):
            flush()
            continue
        if group and (
            row["key"] != group[0]["key"] or line - group[0]["line"] >= 200
        ):
            flush()
        group.append(row)
    flush()
    return output


def scan(
    conn: sqlite3.Connection, policy: ReviewPolicy, project: str | None
) -> HistoryScan:
    """Validate physical source bytes without admitting or launching work."""
    items: list[HistoryItem] = []
    excluded: list[dict[str, str | int]] = []
    for source in conn.execute(
        "SELECT * FROM source ORDER BY provider,thread_id"
    ).fetchall():
        for item in _windows(conn, source, policy, project):
            try:
                budget = ReadBudget(policy.max_input_bytes)
                check_window(
                    conn, item.window, item.scope, policy, budget=budget
                )
                expected = {
                    row["line"]: row["line_sha256"]
                    for row in conn.execute(
                        "SELECT DISTINCT line,line_sha256 FROM event "
                        "WHERE source_id=? AND line BETWEEN ? AND ?",
                        (
                            source["id"],
                            item.window.from_line,
                            item.window.through_line,
                        ),
                    )
                }
                for line, raw, _ in originals(
                    conn,
                    source,
                    item.window.from_line,
                    item.window.through_line,
                    policy.max_input_bytes,
                    budget=budget,
                ):
                    if (
                        line in expected
                        and classify.record_hash(raw) != expected[line]
                    ):
                        raise ValueError("evidence_changed")
            except ValueError as exc:
                code = (
                    str(exc)
                    if str(exc)
                    in {
                        "source_unavailable",
                        "original_unavailable",
                        "evidence_changed",
                        "evidence_erased",
                        "incomplete_window",
                        "evidence_limit",
                        "rejected_scope",
                        "ancestry_unavailable",
                    }
                    else "source_unverifiable"
                )
                excluded.append(
                    {
                        "provider": source["provider"],
                        "thread_id": source["thread_id"],
                        "from_line": item.window.from_line,
                        "through_line": item.window.through_line,
                        "code": code,
                        "cause": _EXCLUDED_CAUSES[code],
                    }
                )
            else:
                items.append(item)
    return HistoryScan(
        policy_digest=digest(policy),
        project_id=project,
        items=items,
        excluded=excluded,
        estimate=estimate(conn, policy, len(items)),
    )


def _from_now(conn: sqlite3.Connection, now: float) -> None:
    """Skip indexed history without claiming that its records were reviewed."""
    for source in conn.execute("SELECT * FROM source").fetchall():
        last, sha = boundary(conn, source)
        conn.execute(
            "INSERT INTO review_source VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider,thread_id) DO UPDATE SET "
            "activation_line=excluded.activation_line,"
            "examined_line=excluded.examined_line,eligible_line=excluded.eligible_line,"
            "completed_line=max(completed_line,excluded.completed_line),boundary_hash=excluded.boundary_hash,updated_at=excluded.updated_at",
            (
                source["provider"],
                source["thread_id"],
                last,
                last,
                last,
                last,
                sha,
                now,
            ),
        )
    conn.execute(
        "UPDATE review_request SET "
        "state='cancelled',retry_at=NULL,cancel_code='scope_from_now' "
        "WHERE state IN ('pending','waiting') "
        "AND trigger IN ('hook','ingest','reconcile','history')"
    )


def configure(
    conn: sqlite3.Connection,
    policy: ReviewPolicy,
    mode: Literal["now", "all"],
    project: str | None,
    now: float,
    confirmation: str | None,
) -> dict[str, object]:
    """Apply one explicit choice after rechecking its exact history scan."""
    require_transaction(conn, now)
    if mode not in ("now", "all"):
        raise ValueError("learning_scope_required")
    policy = checked_policy(policy)
    if mode == "now" and (confirmation is not None or project is not None):
        raise ValueError("unexpected_scope_option")
    if (
        project is not None
        and not conn.execute(
            "SELECT 1 FROM scope WHERE key=? AND kind!='global'", (project,)
        ).fetchone()
    ):
        raise ValueError("rejected_scope")
    previous = review_state.selection(conn)
    if (
        previous is not None
        and previous.mode == mode
        and previous.project_id == project
        and previous.scan_digest == confirmation
    ):
        return {"mode": mode, "enqueued": 0}
    if conn.execute(
        "SELECT 1 FROM review_request WHERE state='running' LIMIT 1"
    ).fetchone():
        raise ValueError("learning_busy")
    selected = scan(conn, policy, project) if mode == "all" else None
    if selected is not None and confirmation != selected.digest:
        raise ValueError("history_scan_changed")
    review_state.ensure(conn)
    conn.execute(
        "INSERT OR REPLACE INTO learning_setup VALUES(1,?,?,?,?)",
        (mode, project, now, confirmation),
    )
    if mode == "now":
        _from_now(conn, now)
    requests = (
        []
        if selected is None
        else [
            enqueue_review(
                conn, item.window, item.scope, policy, "history", now
            ).id
            for item in selected.items
        ]
    )
    return {
        "mode": mode,
        "enqueued": len(requests),
        "project_id": project,
        "excluded_spans": 0 if selected is None else len(selected.excluded),
    }
