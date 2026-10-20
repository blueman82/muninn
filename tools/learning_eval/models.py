"""Define strict offline assessment records without execution authority."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from muninn.knowledge_models import (
    Digest,
    Provider,
    SchemaVersion,
    Scope,
    StrictModel,
    Text,
    UtcSeconds,
)
from muninn.review_evidence_models import (
    EvidenceRecord,
    NonnegativeInt,
    PositiveInt,
)
from muninn.review_models import (
    ObservedConfig,
    PilotBudget,
    ReviewPolicy,
    Usage,
)

type Comparator = Literal[
    "current_muninn", "hermes", "new_present", "new_withheld"
]
type AssessmentStatus = Literal[
    "accept", "revise", "inconclusive", "unverified"
]
type Finite = Annotated[float, Field(allow_inf_nan=False)]
type Measurement = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Confidence = Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)]


SAVE_METRICS = frozenset(
    {
        "unsupported_saves",
        "missed_lessons",
        "no_save_errors",
        "unnecessary_changes",
        "wrong_scope_saves",
        "scope_save_errors",
        "correction_errors",
    }
)


METRIC_UNITS = {
    **dict.fromkeys(SAVE_METRICS, "proportion"),
    **dict.fromkeys(
        (
            "retrieval_useful",
            "retrieval_missed",
            "retrieval_irrelevant",
            "wrong_scope_uses",
            "scope_use_errors",
            "native_capture",
            "recovered_capture",
        ),
        "proportion",
    ),
    **dict.fromkeys(
        (
            "learning_delay",
            "foreground_elapsed",
            "review_elapsed",
            "queue_age",
        ),
        "seconds",
    ),
    **dict.fromkeys(
        ("input_tokens", "output_tokens", "cached_tokens"), "tokens"
    ),
    "cost_decimal": "cost",
    "peak_memory_bytes": "bytes",
    "later_task": "paired_difference",
}


def validate_metric_name(metric: str, unit: str) -> None:
    """Refuse unsupported names and units rather than infer label semantics."""
    if METRIC_UNITS.get(metric) != unit:
        raise ValueError("unsupported_metric")


class ExpectedOutcome(StrictModel):
    """Carry independent reference labels and their evidence provenance."""

    eligible_claim_keys: tuple[Text, ...]
    must_not_save_claim_keys: tuple[Text, ...]
    permitted_scope: Scope
    retrieval_claim_keys: tuple[Text, ...]
    later_task_success_rule: Text | None
    labeler_id: Text
    label_evidence_paths: tuple[Path, ...]

    @model_validator(mode="after")
    def consistent_labels(self) -> Self:
        """Reject contradictory labels and absent reference provenance."""
        if set(self.eligible_claim_keys) & set(self.must_not_save_claim_keys):
            raise ValueError("contradictory_labels")
        if not self.label_evidence_paths:
            raise ValueError("missing_label_evidence")
        return self


class Case(StrictModel):
    """Present one evidence window and independently labelled expectation."""

    id: Text
    family_id: Text
    partition: Literal["pilot", "heldout"]
    chronology: NonnegativeInt
    evidence: tuple[EvidenceRecord, ...]
    later_task: Text | None
    expected: ExpectedOutcome


class IndependentJudgment(StrictModel):
    """Keep independently counted outcomes distinct from model claims."""

    grader_id: Text
    method: Literal["human", "frozen_rule"]
    grader_version: Text
    evidence_paths: tuple[Path, ...]
    numerators: dict[Text, NonnegativeInt]
    denominators: dict[Text, NonnegativeInt]
    later_task_passed: bool | None

    @model_validator(mode="after")
    def valid_counts(self) -> Self:
        """Require matching metric keys, bounded counts and provenance."""
        if self.numerators.keys() != self.denominators.keys():
            raise ValueError("metric_keys_mismatch")
        if any(n > self.denominators[k] for k, n in self.numerators.items()):
            raise ValueError("numerator_exceeds_denominator")
        if not self.evidence_paths:
            raise ValueError("missing_grader_evidence")
        return self


class Conditions(StrictModel):
    """Keep unreported experimental conditions explicitly unknown."""

    evidence_available_digest: Digest | None
    evidence_selected_digest: Digest | None
    native_state: Text | None
    cache_state: Text | None
    approval_mode: Text | None


class Trial(StrictModel):
    """Retain every scheduled outcome and explicitly unreported usage."""

    pair_id: Digest | None = None
    learned_state_digest: Digest | None = None
    id: Text
    case_id: Text
    comparator: Comparator
    policy_digest: Digest
    budget_account_id: Text
    expected_conditions: Conditions | None = None
    observed_conditions: Conditions | None = None
    requested: ObservedConfig
    observed: ObservedConfig
    outcome: Text
    usage: Usage
    timings_seconds: dict[Text, Measurement | None]
    resource_measurements: dict[Text, Measurement | None]
    judgment: IndependentJudgment | None
    evidence_paths: tuple[Path, ...]


class MetricCriterion(StrictModel):
    """Specify explicit thresholds and uncertainty choices before scoring."""

    metric: Text
    unit: Text
    direction: Literal["at_most", "at_least", "equal"]
    threshold: Finite
    interval_method: Literal["exact_binomial", "family_bootstrap", "none"]
    confidence: Confidence | None

    @model_validator(mode="after")
    def interval_confidence(self) -> Self:
        """Require confidence exactly when an interval is requested."""
        validate_metric_name(self.metric, self.unit)
        if (self.interval_method == "none") != (self.confidence is None):
            raise ValueError("invalid_interval_confidence")
        if (
            self.interval_method == "exact_binomial"
            and self.unit != "proportion"
        ):
            raise ValueError("binomial_requires_proportion")
        return self


class HostRequirement(StrictModel):
    """Identify one required host without guessed installed versions."""

    id: Text
    os: Text
    architecture: Text
    host_surface: Text
    provider: Provider
    service: Text
    required_events: tuple[Text, ...]


class HostResult(StrictModel):
    """Keep native delivery separate from recovered indexed evidence."""

    requirement: HostRequirement
    os_version: Text | None
    host_version: Text | None
    provider_version: Text | None
    service_version: Text | None
    trust_identity: Text | None
    native_events: tuple[Text, ...]
    recovered_events: tuple[Text, ...]
    status: AssessmentStatus
    failure_codes: tuple[Text, ...]
    evidence_paths: tuple[Path, ...]

    @model_validator(mode="after")
    def accepted_proof(self) -> Self:
        """Refuse acceptance based on missing versions or recovered events."""
        proof = (
            self.os_version,
            self.host_version,
            self.provider_version,
            self.service_version,
            self.trust_identity,
            self.evidence_paths,
        )
        if self.status == "accept" and (
            not all(proof)
            or self.failure_codes
            or not set(self.requirement.required_events)
            <= set(self.native_events)
        ):
            raise ValueError("missing_native_host_proof")
        return self


class Assessment(StrictModel):
    """Freeze explicit population, scoring, budget and approval metadata."""

    schema_version: SchemaVersion
    id: Text
    phase: Literal["pilot", "heldout"]
    required_hosts: tuple[HostRequirement, ...]
    policies: tuple[ReviewPolicy, ...]
    budget: PilotBudget
    criteria: tuple[MetricCriterion, ...]
    comparators: tuple[Comparator, ...]
    comparator_config_digests: dict[Comparator, Digest]
    sample_size: PositiveInt
    repetitions: PositiveInt
    bootstrap_repetitions: PositiveInt
    seed: NonnegativeInt
    grader_ids: tuple[Text, ...]
    labels_digest: Digest
    cases_digest: Digest
    heldout_family_digest: Digest
    config_digest: Digest
    frozen_at: UtcSeconds | None
    approved_by: Text
    approved_at: UtcSeconds

    @model_validator(mode="after")
    def complete_settings(self) -> Self:
        """Refuse incomplete freeze metadata and inconsistent accounts."""
        if self.phase == "heldout" and (
            self.frozen_at is None or not self.criteria or not self.policies
        ):
            raise ValueError("freeze_required")
        groups = (
            self.comparators,
            self.grader_ids,
            tuple(h.id for h in self.required_hosts),
            tuple(c.metric for c in self.criteria),
        )
        if any(len(g) != len(set(g)) for g in groups):
            raise ValueError("duplicate_setting")
        if (
            not self.comparators
            or not self.grader_ids
            or not self.required_hosts
        ):
            raise ValueError("incomplete_settings")
        if set(self.comparators) != self.comparator_config_digests.keys():
            raise ValueError("comparator_config_mismatch")
        if any(
            p.budget.account_id != self.budget.account_id
            for p in self.policies
        ):
            raise ValueError("budget_account_mismatch")
        return self


class TrialAuthorization(StrictModel):
    """Represent harness authorization without granting runtime admission."""

    assessment_id: Text
    candidate_digest: Digest
    config_digest: Digest
    budget: PilotBudget
    disposable_home: Path
    approved_by: Text
    approved_at: UtcSeconds


class MetricResult(StrictModel):
    """Present counts and unknown measurements without synthetic zeroes."""

    metric: Text
    comparator: Comparator
    numerator: NonnegativeInt | None
    denominator: NonnegativeInt | None
    value: Finite | None
    interval_low: Finite | None
    interval_high: Finite | None
    sample_count: NonnegativeInt
    status: AssessmentStatus
    reason_code: Text | None

    @model_validator(mode="after")
    def undefined_rate(self) -> Self:
        """Forbid defined zero-denominator rates and inverted intervals."""
        if self.denominator == 0 and (
            self.value is not None
            or self.interval_low is not None
            or self.interval_high is not None
            or self.status != "inconclusive"
        ):
            raise ValueError("defined_zero_denominator")
        if (
            self.numerator is not None
            and self.denominator is not None
            and self.numerator > self.denominator
        ):
            raise ValueError("numerator_exceeds_denominator")
        if (self.interval_low is None) != (self.interval_high is None):
            raise ValueError("incomplete_interval")
        if (
            self.interval_low is not None
            and self.interval_high is not None
            and self.interval_low > self.interval_high
        ):
            raise ValueError("inverted_interval")
        return self


class EvaluationReport(StrictModel):
    """Present evidence-bounded results with required hosts and blockers."""

    schema_version: SchemaVersion
    assessment_id: Text
    config_digest: Digest
    status: AssessmentStatus
    metrics: tuple[MetricResult, ...]
    host_results: tuple[HostResult, ...]
    trial_ids: tuple[Text, ...]
    release_blockers: tuple[Text, ...]
    limitations: tuple[Text, ...]
    evidence_paths: tuple[Path, ...]


class CaseFile(StrictModel):
    """Keep synthetic case provenance and uncertain labels explicit."""

    schema_version: SchemaVersion
    synthetic: Literal[True]
    source: Text
    families: dict[Text, Text]
    uncertain_case_ids: tuple[Text, ...]
    cases: tuple[Case, ...]


class OfflineManifest(StrictModel):
    """Provide offline input records without a process execution seam."""

    assessment: Assessment
    cases_path: Path
    trials: tuple[Trial, ...]
    host_results: tuple[HostResult, ...]
    synthetic: Literal[True]
