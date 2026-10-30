"""Define exact candidate, display and historical question contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from muninn.knowledge_models import (
    AffectedRevision,
    Digest,
    EvidenceId,
    RequestId,
    ReviewResult,
    Scope,
    StrictModel,
    Text,
    UnresolvedId,
    UnresolvedProposal,
)
from muninn.review_models import Receipt

type DecisionId = Annotated[str, Field(pattern=r"^D_[0-9a-f]{32}$")]
type DecisionRevision = Annotated[int, Field(strict=True, gt=0)]
type DecisionState = Literal[
    "waiting_verification",
    "approved_pending",
    "saved",
    "rejected",
    "blocked",
    "superseded",
    "erased",
]
type DecisionCode = Literal[
    "decision_invalid",
    "decision_unknown",
    "decision_erased",
    "decision_display_limit",
    "unresolved_invalid",
    "unresolved_unknown",
    "rejected_validation",
    "rejected_evidence",
    "rejected_scope",
    "rejected_revision",
    "rejected_authority",
    "rejected_erased",
    "rejected_fenced",
    "evidence_limit",
    "waiting_index",
    "owner_proof",
    "owner_intent",
    "display_proof",
    "candidate_changed",
    "learning_paused",
]


class QuestionDisposition(StrictModel):
    """Freeze the proposed answer and its exact affected revisions."""

    unresolved_id: UnresolvedId
    answer: Text
    affected: tuple[AffectedRevision, ...]
    complete: bool


class CandidateInput(StrictModel):
    """Attach a complete draft to its checked preparation request."""

    request_id: RequestId
    result: ReviewResult
    disposition: QuestionDisposition | None


class Candidate(StrictModel):
    """Freeze normalized changes and the disclosed support transformation."""

    id: DecisionId
    result: ReviewResult
    scope: Scope
    original_evidence_ids: tuple[EvidenceId, ...]
    targets: tuple[AffectedRevision, ...]
    normalization_version: Literal[1]
    enrichment_version: Literal[1]
    disposition: QuestionDisposition | None
    digest: Digest


class DecisionView(StrictModel):
    """Separate actual candidate state from unverified submitted intent."""

    id: DecisionId
    digest: Digest
    state: DecisionState
    revision: Annotated[int, Field(strict=True, ge=0)]
    candidate: Candidate | None
    receipt: Receipt | None
    code: DecisionCode | None
    intent_state: Literal["waiting_verification", "verified", "blocked"] | None


class DecisionDisplay(StrictModel):
    """Carry one complete, integrity-checked display without truncation."""

    id: DecisionId
    digest: Digest
    text: str
    complete: Literal[True]


class UnresolvedView(StrictModel):
    """Show historical question state without granting evidence admission."""

    id: UnresolvedId
    request_id: RequestId
    state: Literal["open", "resolved", "erased"]
    item: UnresolvedProposal | None
    affected: tuple[AffectedRevision, ...]
    stale_affected: tuple[AffectedRevision, ...]
    support_ids: tuple[EvidenceId, ...]
    scope: Scope
