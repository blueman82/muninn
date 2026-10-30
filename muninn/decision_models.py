"""Define exact candidate, display and historical question contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

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
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceSelector,
    EvidenceWindow,
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
    "decision_reference_required",
    "already_saved",
    "evidence_byte_limit",
    "evidence_record_limit",
    "evidence_work_limit",
    "evidence_selection_limit",
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


class DecisionIntent(StrictModel):
    """Bind a proposed action to exact original owner and display fields."""

    candidate_id: DecisionId
    action: Literal["approve", "reject", "edit"]
    owner_locator: EvidenceLocator
    owner_selector: EvidenceSelector
    display_locator: EvidenceLocator
    display_selector: EvidenceSelector
    replacement_id: DecisionId | None

    @model_validator(mode="after")
    def replacement(self) -> Self:
        """Require a distinct replacement identity only for an edit."""
        if (self.action == "edit") != (
            self.replacement_id is not None
        ) or self.replacement_id == self.candidate_id:
            raise ValueError("decision_invalid")
        return self


class VerifiedDecision(StrictModel):
    """Carry physical verification results for internal orchestration."""

    candidate_id: DecisionId
    candidate_digest: Digest
    action: Literal["approve", "reject", "edit"]
    owner_evidence_id: EvidenceId
    display_evidence_id: EvidenceId
    owner_window: EvidenceWindow
    replacement_id: DecisionId | None
    replacement_digest: Digest | None

    @model_validator(mode="after")
    def replacement(self) -> Self:
        """Keep replacement identity and frozen digest paired for edits."""
        if (
            (self.action == "edit")
            != (
                self.replacement_id is not None
                and self.replacement_digest is not None
            )
            or (self.replacement_id is None)
            != (self.replacement_digest is None)
            or self.replacement_id == self.candidate_id
        ):
            raise ValueError("decision_invalid")
        return self
