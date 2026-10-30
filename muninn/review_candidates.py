"""Validate candidate batches before deferring a removable proposal failure."""

from __future__ import annotations

import sqlite3
from typing import Literal

from muninn.knowledge_models import (
    AffectedRevision,
    Change,
    EvidenceId,
    ReviewResult,
    Scope,
    UnresolvedProposal,
)
from muninn.knowledge_write import body_of, claims, target
from muninn.review_correction import CorrectionFeedback
from muninn.review_evidence_models import EvidenceRecord


class CommitRejectedError(ValueError):
    """Expose a finite safe rejection code without governed validation text."""


class ProposalRejectedError(CommitRejectedError):
    """Attach removal feedback to the unchanged terminal failure code."""

    def __init__(
        self,
        code: Literal[
            "candidate_scope",
            "owner_support",
            "duplicate_target",
            "target_overlap",
        ],
        collection: Literal["changes", "unresolved"],
        index: int,
    ) -> None:
        """Build finite feedback without candidate content or diagnostics."""
        self.feedback = CorrectionFeedback(
            kind="reduction", code=code, collection=collection, index=index
        )
        self.code = {
            "candidate_scope": "rejected_scope",
            "owner_support": "rejected_authority",
            "duplicate_target": "rejected_revision",
            "target_overlap": "rejected_revision",
        }[code]
        super().__init__(self.code)


def _candidate_failure(
    failure: ProposalRejectedError, defer_candidate_failure: bool
) -> ProposalRejectedError:
    """Preserve immediate failure unless the caller permits a terminal scan."""
    if not defer_candidate_failure:
        raise failure
    return failure


def _target(
    conn: sqlite3.Connection, ref: AffectedRevision, scope: Scope
) -> sqlite3.Row:
    """Convert expected target refusals without swallowing systemic errors."""
    try:
        return target(conn, ref, scope)
    except ValueError as exc:
        if str(exc) not in {"rejected_revision", "rejected_scope"}:
            raise
        raise CommitRejectedError(str(exc)) from None


def _support(
    refs: list[EvidenceId], records: dict[EvidenceId, EvidenceRecord]
) -> bool:
    """Reject unknown references and report attributable owner support."""
    if not set(refs) <= records.keys():
        raise CommitRejectedError("rejected_evidence")
    return any(
        records[r].authority == "direct_owner"
        and records[r].evidence_type == "owner_statement"
        for r in refs
    )


def _change(
    conn: sqlite3.Connection,
    change: Change,
    scope: Scope,
    records: dict[EvidenceId, EvidenceRecord],
    replaced: set[str],
    index: int,
    defer_candidate_failure: bool,
) -> ProposalRejectedError | None:
    """Raise immediately or retain feedback for the complete terminal scan."""
    failure = (
        _candidate_failure(
            ProposalRejectedError("candidate_scope", "changes", index),
            defer_candidate_failure,
        )
        if change.scope != scope
        else None
    )
    owner = (
        change.kind in {"preference", "decision"} or scope.project_id is None
    )
    if change.action == "replace":
        if change.knowledge_id in replaced:
            failure = failure or _candidate_failure(
                ProposalRejectedError("duplicate_target", "changes", index),
                defer_candidate_failure,
            )
        replaced.add(change.knowledge_id)
        old = _target(
            conn,
            AffectedRevision(
                knowledge_id=change.knowledge_id,
                revision=change.expected_revision,
            ),
            scope,
        )
        owner = owner or old["kind"] in {"preference", "decision"}
    for _, _, refs, _ in claims(body_of(change)):
        supported = _support(refs, records)
        if owner and not supported:
            failure = failure or _candidate_failure(
                ProposalRejectedError("owner_support", "changes", index),
                defer_candidate_failure,
            )
    return failure


def _unresolved(
    conn: sqlite3.Connection,
    proposal: UnresolvedProposal,
    scope: Scope,
    records: dict[EvidenceId, EvidenceRecord],
    replaced: set[str],
    index: int,
    defer_candidate_failure: bool,
) -> ProposalRejectedError | None:
    """Keep overlap removable only after terminal affected-target checks."""
    _support(proposal.evidence_refs, records)
    seen: set[str] = set()
    failure = None
    for affected in proposal.affected:
        if affected.knowledge_id in seen:
            raise CommitRejectedError("rejected_revision")
        seen.add(affected.knowledge_id)
        if affected.knowledge_id in replaced:
            failure = failure or _candidate_failure(
                ProposalRejectedError("target_overlap", "unresolved", index),
                defer_candidate_failure,
            )
        _target(conn, affected, scope)
    return failure


def validate_batch(
    conn: sqlite3.Connection,
    result: ReviewResult,
    scope: Scope,
    evidence: tuple[EvidenceRecord, ...],
    *,
    defer_candidate_failure: bool,
) -> None:
    """Defer eligible failures for a terminal scan only when opted in."""
    records = {r.id: r for r in evidence}
    replaced: set[str] = set()
    failure = None
    for index, change in enumerate(result.changes):
        candidate = _change(
            conn,
            change,
            scope,
            records,
            replaced,
            index,
            defer_candidate_failure,
        )
        failure = failure or candidate
    for index, proposal in enumerate(result.unresolved):
        candidate = _unresolved(
            conn,
            proposal,
            scope,
            records,
            replaced,
            index,
            defer_candidate_failure,
        )
        failure = failure or candidate
    if failure is not None:
        raise failure
