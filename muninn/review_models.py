"""Define data-only review lifecycle, policy and presentation contracts."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from muninn.knowledge_models import (
    AffectedRevision,
    AttemptId,
    Digest,
    EvidenceId,
    RequestId,
    ReviewResult,
    SchemaVersion,
    Scope,
    StrictModel,
    Text,
    UnresolvedId,
    UtcSeconds,
)
from muninn.review_evidence_models import (
    EvidenceRecord,
    EvidenceWindow,
    NonnegativeInt,
    PositiveInt,
)


def nonnegative_decimal(value: str) -> str:
    """Reject invalid or negative reported decimal amounts."""
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid_cost_decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid_cost_decimal")
    return value


type CostDecimal = Annotated[Text, AfterValidator(nonnegative_decimal)]
type PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
type RequestState = Literal[
    "pending", "running", "waiting", "completed", "failed", "cancelled"
]
type CallState = Literal["reserved", "launched", "completed", "unknown"]


class Usage(StrictModel):
    """Preserve unknown usage while constraining reported values."""

    input_tokens: NonnegativeInt | None
    output_tokens: NonnegativeInt | None
    cached_tokens: NonnegativeInt | None
    cost_decimal: CostDecimal | None
    currency: Text | None


class RunnerConfig(StrictModel):
    """Require every requested runner setting explicitly."""

    runner: Literal["claude_cli"]
    executable: Text
    version: Text
    model: Text
    reasoning: Text


class ObservedConfig(StrictModel):
    """Preserve null for every unreported runner setting."""

    runner: Text | None
    version: Text | None
    model: Text | None
    reasoning: Text | None


class PilotBudget(StrictModel):
    """Carry explicit approved bounds for a persistent budget account."""

    account_id: Text
    max_model_calls: PositiveInt
    max_wall_seconds: PositiveSeconds
    max_cost_decimal: CostDecimal | None
    conservative_call_cost_decimal: CostDecimal | None
    currency: Text | None
    approved_by: Text
    approved_at: UtcSeconds

    @model_validator(mode="after")
    def cost_currency(self) -> Self:
        """Require currency for either explicit monetary bound."""
        if (
            self.max_cost_decimal is not None
            or self.conservative_call_cost_decimal is not None
        ) and self.currency is None:
            raise ValueError("missing_budget_currency")
        return self


class ReviewPolicy(StrictModel):
    """Require explicit model, boundary, retry and budget controls."""

    schema_version: SchemaVersion
    policy_id: Text
    prompt_digest: Digest
    projection_version: Text
    result_schema_digest: Digest
    runner: RunnerConfig
    max_rounds: PositiveInt
    max_input_bytes: PositiveInt
    max_output_bytes: PositiveInt
    context_bytes: PositiveInt
    deadline_seconds: PositiveSeconds
    max_attempts: PositiveInt
    retry_backoff_seconds: list[UtcSeconds]
    concurrency: PositiveInt
    hook_wait_seconds: UtcSeconds
    allow_global: bool
    budget: PilotBudget

    @model_validator(mode="after")
    def retry_schedule(self) -> Self:
        """Require one explicit backoff for each possible retry."""
        if len(self.retry_backoff_seconds) != self.max_attempts - 1:
            raise ValueError("invalid_backoff_count")
        return self


class RequestView(StrictModel):
    """Present an admitted request and its program-owned lifecycle."""

    schema_version: SchemaVersion
    id: RequestId
    window: EvidenceWindow
    scope: Scope
    policy_digest: Digest
    state: RequestState
    current_attempt_id: AttemptId | None
    created_at: UtcSeconds
    retry_at: UtcSeconds | None
    accepted: bool


class AttemptView(StrictModel):
    """Present requested and observed configuration for one attempt."""

    schema_version: SchemaVersion
    id: AttemptId
    request_id: RequestId
    generation: PositiveInt
    requested: RunnerConfig | None
    observed: ObservedConfig
    started_at: UtcSeconds
    progress_at: UtcSeconds
    ended_at: UtcSeconds | None
    outcome: Text | None
    error_code: Text | None
    usage: Usage | None


class ProcessIdentity(StrictModel):
    """Carry program-verified native creation and process-group identity."""

    pid: PositiveInt
    created: Text
    pgid: PositiveInt | None
    job_marker: Text | None


class CallView(StrictModel):
    """Present one reservation and launch without prompt or output text."""

    attempt_id: AttemptId
    round_index: PositiveInt
    budget_account_id: Text
    policy_digest: Digest
    state: CallState
    reserved_at: UtcSeconds
    launched_at: UtcSeconds | None
    ended_at: UtcSeconds | None
    requested: RunnerConfig
    observed: ObservedConfig
    usage: Usage | None
    cost_reservation_decimal: CostDecimal | None
    currency: Text | None
    process_identity: ProcessIdentity | None
    outcome: Text | None


class Receipt(StrictModel):
    """Present an atomic commit result with program-allocated identities."""

    schema_version: SchemaVersion
    request_id: RequestId
    attempt_id: AttemptId
    applied: list[AffectedRevision]
    unresolved_ids: list[UnresolvedId]
    committed_at: UtcSeconds
    outcome: Literal["completed", "completed_unresolved", "no_change"]
    no_change_reason: Text | None

    @model_validator(mode="after")
    def outcome_reason(self) -> Self:
        """Keep no-change receipts empty while allowing an erased reason."""
        if self.outcome == "no_change":
            if self.applied or self.unresolved_ids:
                raise ValueError("invalid_no_change_receipt")
        elif self.no_change_reason is not None:
            raise ValueError("unexpected_receipt_reason")
        return self


class ProposalInput(StrictModel):
    """Attach a checked foreground proposal to an admitted request."""

    request_id: RequestId
    result: ReviewResult


class ReviewContext(StrictModel):
    """Share admitted data between review runtime and budget consumers."""

    request_id: RequestId
    attempt_id: AttemptId
    generation: PositiveInt
    scope: Scope
    policy: ReviewPolicy
    admitted_windows: tuple[EvidenceWindow, ...]
    evidence_ids: tuple[EvidenceId, ...]
    round_index: PositiveInt
    deadline: PositiveSeconds


class ReviewDetail(StrictModel):
    """Present request history and evidence through data-only imports."""

    request: RequestView
    attempts: list[AttemptView]
    calls: list[CallView]
    evidence: list[EvidenceRecord]
    receipt: Receipt | None


class ReviewStats(StrictModel):
    """Present nonnegative counts and explicitly incomplete busy coverage."""

    schema_version: SchemaVersion
    requests_by_state: dict[RequestState, NonnegativeInt]
    calls_by_state: dict[CallState, NonnegativeInt]
    oldest_pending_age_seconds: UtcSeconds | None
    last_progress_age_seconds: UtcSeconds | None
    eligible_records: NonnegativeInt
    completed_records: NonnegativeInt
    unresolved_items: NonnegativeInt
    usage: Usage
    observed_enqueue_busy: NonnegativeInt | None
    busy_count_complete: bool
