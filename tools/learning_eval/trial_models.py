"""Define harness-only candidates and content-free durable call records."""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path
from typing import Literal

from muninn.knowledge_models import (
    AttemptId,
    Digest,
    SchemaVersion,
    StrictModel,
    Text,
    UtcSeconds,
)
from muninn.review_charge import Charge
from muninn.review_evidence_models import PositiveInt
from muninn.review_json import canonical_json
from muninn.review_models import (
    CallState,
    CostDecimal,
    ProcessIdentity,
    RunnerConfig,
    Usage,
)
from tools.learning_eval.cases import config_digest, digest
from tools.learning_eval.models import (
    Assessment,
    Comparator,
    TrialAuthorization,
)


class Candidate(StrictModel):
    """Bind static inputs without asserting observed native coverage."""

    schema_version: SchemaVersion
    kind: Literal["synthetic_candidate"]
    recipe: Literal["claude-static-posix-v1"]
    assessment_id: Text
    budget_account_id: Text
    requested: RunnerConfig
    executable_sha256: Digest
    schema_digest: Digest
    output_contract_digest: Digest
    cli_cwd: Path
    config_roots: tuple[Path, ...]
    environment_sha256: dict[str, Digest]
    config_digest: Digest


class ScheduledCall(StrictModel):
    """Bind one model call to a scheduled comparator, retry and round."""

    id: AttemptId
    assessment_id: Text
    comparator: Comparator
    trial_id: Text
    retry_index: PositiveInt
    round_index: PositiveInt
    policy_digest: Digest
    config_digest: Digest
    authorization_digest: Digest


class AccountCall(StrictModel):
    """Retain one spent claim and nullable usage without transcript data."""

    schedule: ScheduledCall
    state: CallState
    reserved_at: UtcSeconds
    updated_at: UtcSeconds
    cost_reservation_decimal: CostDecimal | None
    currency: Text | None
    process_identity: ProcessIdentity | None = None
    activation_claimed: bool = False
    cleaned: bool = False
    usage: Usage | None = None
    outcome: Text | None = None

    def charge(self) -> Charge:
        """Project accounting through the shared charge arithmetic."""
        return Charge(
            self.state,
            self.reserved_at,
            self.usage,
            self.cost_reservation_decimal,
            self.currency,
        )


def checked_assessment(assessment: Assessment) -> Assessment:
    """Require strict self-consistency without granting heldout read access."""
    value = Assessment.model_validate_json(canonical_json(assessment))
    if config_digest(value) != value.config_digest or not value.policies:
        raise ValueError("assessment_binding_mismatch")
    return value


def check_time(
    now: float, calls: tuple[AccountCall, ...], observed_at: object
) -> None:
    """Refuse nonfinite timestamps and reversals after any account event."""
    if (
        not isinstance(observed_at, (int, float))
        or not math.isfinite(observed_at)
        or observed_at < 0
    ):
        raise ValueError("account_invalid")
    if not math.isfinite(now) or now < observed_at:
        raise ValueError("budget_clock_reversed")
    if calls and now < max(c.updated_at for c in calls):
        raise ValueError("budget_clock_reversed")


def reserved_charge(assessment: Assessment, policy_digest: str) -> str | None:
    """Reserve the stronger account and current-policy call cost."""
    policy = next(
        p
        for p in assessment.policies
        if digest(p.model_dump(mode="json")) == policy_digest
    )
    costs = (
        assessment.budget.conservative_call_cost_decimal,
        policy.budget.conservative_call_cost_decimal,
    )
    if None in costs:
        return None
    return str(max(Decimal(cost) for cost in costs if cost is not None))


def validate_schedule(
    assessment: Assessment,
    path: Path,
    schedule: ScheduledCall,
    authorization: TrialAuthorization,
) -> None:
    """Bind an immutable schedule and authority to the account location."""
    schedule = ScheduledCall.model_validate_json(canonical_json(schedule))
    authorization = TrialAuthorization.model_validate_json(
        canonical_json(authorization)
    )
    value = assessment
    if (
        schedule.assessment_id != value.id
        or authorization.assessment_id != value.id
        or schedule.comparator not in value.comparators
        or schedule.config_digest != value.config_digest
        or schedule.authorization_digest
        != digest(authorization.model_dump(mode="json"))
        or authorization.budget != value.budget
        or authorization.approved_by != value.approved_by
        or authorization.approved_at != value.approved_at
    ):
        raise ValueError("authorization_mismatch")
    if (
        path == authorization.disposable_home
        or authorization.disposable_home in path.parents
    ):
        raise ValueError("account_disposable")
    policies = {digest(p.model_dump(mode="json")): p for p in value.policies}
    policy = policies.get(schedule.policy_digest)
    if (
        policy is None
        or schedule.round_index > policy.max_rounds
        or schedule.retry_index > policy.max_attempts
    ):
        raise ValueError("schedule_policy_mismatch")
