"""Test shortcuts that compose Muninn review and decision functions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from muninn import decision_requests, review_catchup, review_evidence, store
from muninn.decision_models import DecisionId
from muninn.decision_write import decision_view
from muninn.knowledge_erase import (
    ErasureSummary,
    erase_support_closure,
    support_closure,
)
from muninn.knowledge_models import EvidenceId
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceRecord,
    EvidenceSelector,
)
from muninn.review_models import RequestView, ReviewPolicy
from muninn.review_originals import originals
from muninn.review_progress import require_transaction
from muninn.review_read_budget import ReadBudget
from muninn.review_selectors import build_record
from muninn.review_settings import POLICY


def select_evidence(
    home: Path, locator: EvidenceLocator, selector: EvidenceSelector
) -> EvidenceRecord:
    """Select a permitted original field; the caller checks admission."""
    with closing(store.connect_ro(store.db_path(home))) as conn:
        source = review_evidence._source(conn, locator.source)
        _, raw, original = originals(conn, source, locator.line, locator.line)[
            0
        ]
        return build_record(conn, source, locator, selector, raw, original)


def erase_review_support(
    conn: sqlite3.Connection, evidence_ids: tuple[EvidenceId, ...], now: float
) -> ErasureSummary:
    """Erase selected evidence and every associated governed derivation."""
    return erase_support_closure(
        conn, support_closure(conn, evidence_ids), now
    )


def admit_application(
    conn: sqlite3.Connection,
    home: Path,
    ident: DecisionId,
    revision: int,
    now: float,
) -> RequestView:
    """Admit one active approval using the caller's writer transaction."""
    require_transaction(conn, now)
    decision_requests._approval(conn, ident, revision)
    return decision_requests._admit(
        conn,
        home,
        decision_view(conn, ident),
        now,
        POLICY,
        ReadBudget(POLICY.max_input_bytes),
    )


def catch_up(
    conn: sqlite3.Connection, policy: ReviewPolicy, now: float
) -> tuple[str, ...]:
    """Run one catch-up turn and return the ids of the new pending requests."""
    before = {row[0] for row in conn.execute("SELECT id FROM review_request")}
    review_catchup.catch_up(conn, policy, now, 0)
    return tuple(
        row[0]
        for row in conn.execute(
            "SELECT id FROM review_request WHERE state='pending' ORDER BY id"
        )
        if row[0] not in before
    )
