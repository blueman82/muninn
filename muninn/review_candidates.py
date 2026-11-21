"""Validate candidate batches before deferring a removable proposal failure."""

from __future__ import annotations

import sqlite3
from typing import Literal, NamedTuple

from muninn.knowledge_models import (
    AffectedRevision,
    Change,
    EvidenceId,
    ReviewResult,
    Scope,
    UnresolvedProposal,
)
from muninn.knowledge_write import body_of, claims, target
from muninn.review_binding import Adjacent, owner_supported
from muninn.review_correction import CorrectionFeedback
from muninn.review_evidence_models import EvidenceRecord
from muninn.review_read_budget import EvidenceLimitError


class OwnerSupport(NamedTuple):
    """Carry how a claim's owner support is checked."""

    adjacent: Adjacent | None = None
    approved_owner: EvidenceId | None = None


_NO_OWNER = OwnerSupport()


class CandidateChecks(NamedTuple):
    """Keep failure deferral and owner support controls independent."""

    defer_failure: bool
    allow_missing_owner: bool
    owner: OwnerSupport


class CommitRejectedError(ValueError):
    """Expose a safe rejection code."""


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
        limit: str | None = None,
    ) -> None:
        """Build finite feedback without candidate content or diagnostics.

        Args:
            code: The removal reason given to the model.
            collection: The list that holds the item.
            index: The item's position in that list.
            limit: An exhausted read allowance code to record in place of
                the code that ``code`` maps to.
        """
        self.feedback = CorrectionFeedback(
            kind="reduction", code=code, collection=collection, index=index
        )
        self.code = {
            "candidate_scope": "rejected_scope",
            "owner_support": "rejected_authority",
            "duplicate_target": "rejected_revision",
            "target_overlap": "rejected_revision",
        }[code]
        self.code = limit or self.code
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


def _known(
    refs: list[EvidenceId], records: dict[EvidenceId, EvidenceRecord]
) -> None:
    """Reject references outside the revalidated evidence."""
    if not set(refs) <= records.keys():
        raise CommitRejectedError("rejected_evidence")


def _change(
    conn: sqlite3.Connection,
    change: Change,
    scope: Scope,
    records: dict[EvidenceId, EvidenceRecord],
    replaced: set[str],
    index: int,
    checks: CandidateChecks,
) -> ProposalRejectedError | None:
    """Raise immediately or retain feedback for the complete terminal scan."""
    failure = (
        _candidate_failure(
            ProposalRejectedError("candidate_scope", "changes", index),
            checks.defer_failure,
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
                checks.defer_failure,
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
        _known(refs, records)
        if (
            not owner
            or checks.allow_missing_owner
            or checks.owner.approved_owner in refs
        ):
            continue
        limit = None
        try:
            supported = owner_supported(refs, records, checks.owner.adjacent)
        except EvidenceLimitError as exc:
            # A pair that could not be read is a limit, not a wrong owner.
            supported, limit = False, exc.code
        if not supported:
            failure = failure or _candidate_failure(
                ProposalRejectedError(
                    "owner_support", "changes", index, limit
                ),
                checks.defer_failure,
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
    _known(proposal.evidence_refs, records)
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
    allow_missing_owner: bool = False,
    owner: OwnerSupport = _NO_OWNER,
) -> None:
    """Defer eligible failures for a terminal scan only when opted in.

    Args:
        conn: Caller-owned transaction with knowledge and source rows.
        result: Proposed changes and unresolved questions.
        scope: The request scope every change must keep.
        evidence: Revalidated evidence cited by the result.
        defer_candidate_failure: Keep removable refusals for a final scan.
        allow_missing_owner: Stage a candidate before any owner decision.
        owner: Agreement gap reader, and the owner reply of a verified
            owner-approved decision being applied, which proves the claims it
            backs.

    Raises:
        CommitRejectedError: If evidence, targets or owner support fail.
    """
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
            CandidateChecks(
                defer_candidate_failure, allow_missing_owner, owner
            ),
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
