"""Score independent offline judgments without inventing absent evidence."""

from __future__ import annotations

import math
import random
from collections import Counter
from statistics import mean

from tools.learning_eval import report as reporting
from tools.learning_eval.cases import (
    validate_partition,
    validate_trial_bindings,
)
from tools.learning_eval.models import (
    SAVE_METRICS,
    Assessment,
    AssessmentStatus,
    Case,
    Comparator,
    EvaluationReport,
    HostResult,
    MetricCriterion,
    MetricResult,
    Trial,
    validate_metric_name,
)

BLOCKERS = frozenset(
    {
        "lost_acknowledged_work",
        "duplicate_commits",
        "unauthorized_stale_saves",
        "erasure_resurrection",
    }
)


def binomial_interval(
    successes: int, total: int, confidence: float
) -> tuple[float, float]:
    """Compute equal-tailed Clopper-Pearson bounds by monotone bisection."""
    alpha = (1 - confidence) / 2

    def cdf(k: int, probability: float) -> float:
        return sum(
            math.exp(
                math.lgamma(total + 1)
                - math.lgamma(i + 1)
                - math.lgamma(total - i + 1)
                + i * math.log(probability)
                + (total - i) * math.log1p(-probability)
            )
            for i in range(k + 1)
        )

    def root(k: int, target: float) -> float:
        low, high = 0.0, 1.0
        for _ in range(60):
            middle = (low + high) / 2
            if middle == 0 or middle == 1:
                break
            if cdf(k, middle) > target:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    return (
        0.0 if successes == 0 else root(successes - 1, 1 - alpha),
        1.0 if successes == total else root(successes, alpha),
    )


def family_interval(
    values: dict[str, list[float]], assessment: Assessment, confidence: float
) -> tuple[float, float]:
    """Resample whole families with an explicit seed and repetition count."""
    rng = random.Random(assessment.seed)
    families = sorted(values)
    samples = sorted(
        mean(
            v
            for family in rng.choices(families, k=len(families))
            for v in values[family]
        )
        for _ in range(assessment.bootstrap_repetitions)
    )
    tail = (1 - confidence) / 2
    return (
        samples[int(tail * (len(samples) - 1))],
        samples[math.ceil((1 - tail) * (len(samples) - 1))],
    )


def criterion_status(
    criterion: MetricCriterion, low: float, high: float
) -> AssessmentStatus:
    """Compare uncertainty bounds with an explicit owner criterion."""
    target = criterion.threshold
    if criterion.direction == "at_most":
        return (
            "accept"
            if high <= target
            else "revise" if low > target else "inconclusive"
        )
    if criterion.direction == "at_least":
        return (
            "accept"
            if low >= target
            else "revise" if high < target else "inconclusive"
        )
    return (
        "accept"
        if low == high == target
        else "revise" if target < low or target > high else "inconclusive"
    )


def checked_trials(
    cases: tuple[Case, ...], trials: tuple[Trial, ...], assessment: Assessment
) -> None:
    """Reject unknown populations, accounts, graders and unfrozen metrics."""
    validate_trial_bindings(trials, assessment)
    by_id = {case.id: case for case in cases}
    if len({t.id for t in trials}) != len(trials):
        raise ValueError("duplicate_trial")
    counts = Counter((t.case_id, t.comparator) for t in trials)
    if any(count > assessment.repetitions for count in counts.values()):
        raise ValueError("unscheduled_trial")
    metrics = {criterion.metric for criterion in assessment.criteria}
    for trial in trials:
        if (
            trial.case_id not in by_id
            or trial.comparator not in assessment.comparators
        ):
            raise ValueError("unknown_trial_population")
        if trial.budget_account_id != assessment.budget.account_id:
            raise ValueError("budget_account_mismatch")
        judgment = trial.judgment
        if judgment is not None:
            if judgment.grader_id not in assessment.grader_ids:
                raise ValueError("unknown_grader")
            identities = {trial.requested.model, trial.observed.model}
            if (
                judgment.grader_id in identities
                or by_id[trial.case_id].expected.labeler_id in identities
            ):
                raise ValueError("grader_not_independent")
            if not judgment.numerators.keys() <= metrics:
                raise ValueError("unfrozen_metric")


def retained_hosts(
    assessment: Assessment, supplied: tuple[HostResult, ...]
) -> tuple[HostResult, ...]:
    """Retain failures and supply unverified rows for absent requirements."""
    required = {host.id: host for host in assessment.required_hosts}
    found: dict[str, HostResult] = {}
    for result in supplied:
        identity = result.requirement.id
        if identity in found or required.get(identity) != result.requirement:
            raise ValueError("host_requirement_mismatch")
        found[identity] = result
    return tuple(
        found.get(host.id)
        or HostResult(
            requirement=host,
            os_version=None,
            host_version=None,
            provider_version=None,
            service_version=None,
            trust_identity=None,
            native_events=(),
            recovered_events=(),
            status="unverified",
            failure_codes=("missing_native_proof",),
            evidence_paths=(),
        )
        for host in assessment.required_hosts
    )


def numeric_value(trial: Trial, metric: str, unit: str) -> float | None:
    """Read reported quantities only, retaining every missing measurement."""
    if unit == "seconds":
        return trial.timings_seconds.get(metric)
    usage = trial.usage.model_dump()
    if unit in {"tokens", "cost"}:
        value = usage.get(metric)
        return float(value) if isinstance(value, (int, float, str)) else None
    return trial.resource_measurements.get(metric)


def paired_values(
    cases: tuple[Case, ...],
    trials: tuple[Trial, ...],
    comparator: Comparator,
) -> tuple[dict[str, list[float]], bool]:
    """Pair judged outcomes only for matched reported settings."""
    if comparator == "new_withheld":
        return {}, False
    families = {
        case.id: case.family_id
        for case in cases
        if case.expected.later_task_success_rule is not None
    }
    pairs: dict[tuple[str, str], dict[str, list[Trial]]] = {}
    complete = True
    for trial in trials:
        if trial.comparator in {comparator, "new_withheld"}:
            if trial.pair_id is None:
                complete = False
                continue
            pairs.setdefault((trial.case_id, trial.pair_id), {}).setdefault(
                trial.comparator, []
            ).append(trial)
    values: dict[str, list[float]] = {}
    covered: set[str] = set()
    for (case_id, _), comparisons in sorted(pairs.items()):
        present = comparisons.get(comparator, [])
        withheld = comparisons.get("new_withheld", [])
        if len(present) != 1 or len(withheld) != 1:
            complete = False
            continue
        left, right = present[0], withheld[0]
        if not comparable_pair(left, right) or case_id not in families:
            complete = False
        elif left.judgment is not None and right.judgment is not None:
            difference = float(bool(left.judgment.later_task_passed)) - float(
                bool(right.judgment.later_task_passed)
            )
            values.setdefault(families[case_id], []).append(difference)
            covered.add(case_id)
    return values, complete and covered == set(families)


def comparable_pair(left: Trial, right: Trial) -> bool:
    """Refuse memory attribution when reported configuration is unmatched."""
    configs = (left.requested, left.observed, right.requested, right.observed)
    judgments = (left.judgment, right.judgment)
    return (
        left.case_id == right.case_id
        and left.pair_id is not None
        and left.pair_id == right.pair_id
        and left.learned_state_digest is not None
        and left.learned_state_digest == right.learned_state_digest
        and (
            reporting.matched_conditions(left)
            and reporting.matched_conditions(right)
            and left.expected_conditions == right.expected_conditions
        )
        and all(
            c == configs[0] and all(c.model_dump().values()) for c in configs
        )
        and all(
            j is not None and j.later_task_passed is not None
            for j in judgments
        )
    )


def metric_values(
    cases: tuple[Case, ...],
    trials: tuple[Trial, ...],
    criterion: MetricCriterion,
    comparator: Comparator,
) -> tuple[dict[str, list[float]], bool]:
    """Collect measurements without dropping unknowns."""
    if criterion.unit == "paired_difference":
        return paired_values(cases, trials, comparator)
    families = {case.id: case.family_id for case in cases}
    values: dict[str, list[float]] = {}
    complete = True
    for trial in trials:
        if trial.comparator != comparator:
            continue
        value = numeric_value(trial, criterion.metric, criterion.unit)
        if value is None:
            complete = False
        else:
            values.setdefault(families[trial.case_id], []).append(value)
    return values, complete


def score_metric(
    cases: tuple[Case, ...],
    trials: tuple[Trial, ...],
    assessment: Assessment,
    criterion: MetricCriterion,
    comparator: Comparator,
) -> MetricResult:
    """Report one frozen metric with counts, uncertainty and missingness."""
    selected = tuple(t for t in trials if t.comparator == comparator)
    scheduled = assessment.sample_size * assessment.repetitions
    complete = len(selected) == scheduled and all(
        t.requested == t.observed and reporting.matched_conditions(t)
        for t in selected
    )
    numerator: int | None = None
    denominator: int | None = None
    value: float | None = None
    interval: tuple[float, float] | None = None
    if criterion.unit == "proportion":
        labelled = {
            c.id
            for c in cases
            if c.expected.eligible_claim_keys
            or c.expected.must_not_save_claim_keys
        }
        judged = [
            t.judgment
            for t in selected
            if (criterion.metric not in SAVE_METRICS or t.case_id in labelled)
            and t.judgment is not None
            and criterion.metric in t.judgment.numerators
        ]
        complete = complete and len(judged) == scheduled
        if judged:
            numerator = sum(j.numerators[criterion.metric] for j in judged)
            denominator = sum(j.denominators[criterion.metric] for j in judged)
            value = numerator / denominator if denominator else None
            if denominator and criterion.confidence is not None:
                if criterion.interval_method != "exact_binomial":
                    raise ValueError("unsupported_proportion_interval")
                interval = binomial_interval(
                    numerator, denominator, criterion.confidence
                )
    else:
        values, known = metric_values(cases, trials, criterion, comparator)
        complete = (
            complete and known and sum(map(len, values.values())) == scheduled
        )
        if values:
            value = mean(v for family in values.values() for v in family)
            if (
                criterion.interval_method == "family_bootstrap"
                and criterion.confidence is not None
            ):
                interval = family_interval(
                    values, assessment, criterion.confidence
                )
    status: AssessmentStatus = "inconclusive"
    reason = (
        "missing_measurement" if value is None else "incomplete_population"
    )
    if value is not None and complete:
        bounds = interval or (value, value)
        status = criterion_status(criterion, *bounds)
        reason = (
            "interval_crosses_threshold" if status == "inconclusive" else None
        )
    return MetricResult(
        metric=criterion.metric,
        comparator=comparator,
        numerator=numerator,
        denominator=denominator,
        value=value,
        interval_low=interval[0] if interval else None,
        interval_high=interval[1] if interval else None,
        sample_count=scheduled,
        status=status,
        reason_code=reason,
    )


def score_trials(
    cases: tuple[Case, ...],
    trials: tuple[Trial, ...],
    assessment: Assessment,
    *,
    host_results: tuple[HostResult, ...] = (),
) -> EvaluationReport:
    """Score every supplied outcome and retain the full required population."""
    for criterion in assessment.criteria:
        validate_metric_name(criterion.metric, criterion.unit)
    validate_partition(cases)
    if any(case.partition != assessment.phase for case in cases):
        raise ValueError("phase_mismatch")
    checked_trials(cases, trials, assessment)
    hosts = retained_hosts(assessment, host_results)
    metrics = tuple(
        score_metric(cases, trials, assessment, criterion, comparator)
        for criterion in assessment.criteria
        for comparator in assessment.comparators
        if criterion.unit != "paired_difference"
        or comparator != "new_withheld"
    )
    blockers = tuple(sorted({trial.outcome for trial in trials} & BLOCKERS))
    limitations, status = reporting.summarize(
        trials, metrics, hosts, blockers, len(cases) == assessment.sample_size
    )
    return EvaluationReport(
        schema_version=1,
        assessment_id=assessment.id,
        config_digest=assessment.config_digest,
        status=status,
        metrics=metrics,
        host_results=hosts,
        trial_ids=tuple(t.id for t in trials),
        release_blockers=blockers,
        limitations=tuple(limitations),
        evidence_paths=tuple(
            sorted({path for t in trials for path in t.evidence_paths})
        ),
    )
