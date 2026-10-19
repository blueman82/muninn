"""Write strict offline reports with explicit null measurements."""

from __future__ import annotations

from pathlib import Path

from muninn.review_json import canonical_json
from tools.learning_eval.account import Account
from tools.learning_eval.models import (
    AssessmentStatus,
    EvaluationReport,
    HostResult,
    MetricResult,
    Trial,
)


def write_report(report: EvaluationReport, path: Path) -> None:
    """Write a validated JSON report including all nullable fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(report) + "\n", encoding="utf-8")


def matched_conditions(trial: Trial) -> bool:
    """Require explicit complete expectations and equal observations."""
    expected = trial.expected_conditions
    return (
        expected is not None
        and all(expected.model_dump().values())
        and expected == trial.observed_conditions
    )


def summarize(
    trials: tuple[Trial, ...],
    metrics: tuple[MetricResult, ...],
    hosts: tuple[HostResult, ...],
    blockers: tuple[str, ...],
    complete: bool,
) -> tuple[list[str], AssessmentStatus]:
    """Keep uncertainty visible and reliability blockers decisive."""
    limitations = ["offline_scoring_does_not_establish_live_acceptance"]
    if any(t.requested != t.observed for t in trials):
        limitations.append(
            "unmatched_configuration_not_attributable_to_memory"
        )
    if any(not matched_conditions(t) for t in trials):
        limitations.append("unmatched_conditions_not_attributable_to_memory")
    uncertain = any(
        t.outcome.startswith("budget_")
        or t.outcome in {"unknown", "account_recovery_required"}
        for t in trials
    )
    if uncertain:
        limitations.append("budget_refusal_or_uncertain_spend")
    if any(t.judgment is None for t in trials):
        limitations.append("missing_independent_judgment")
    if not complete:
        limitations.append("incomplete_case_population")
    statuses: set[str] = {metric.status for metric in metrics} | {
        host.status for host in hosts
    }
    return limitations, report_status(
        statuses, bool(blockers), uncertain, bool(metrics) and complete
    )


def report_status(
    statuses: set[str],
    blocked: bool,
    uncertain: bool,
    complete: bool,
) -> AssessmentStatus:
    """Prevent budget refusal from being mistaken for measured acceptance."""
    if blocked:
        return "revise"
    if uncertain:
        return "unverified" if "unverified" in statuses else "inconclusive"
    if "revise" in statuses:
        return "revise"
    if "unverified" in statuses:
        return "unverified"
    if complete and statuses == {"accept"}:
        return "accept"
    return "inconclusive"


def with_account(
    report: EvaluationReport, account: Account
) -> EvaluationReport:
    """Attach retained schedules and uncertainty without claiming proof."""
    if (
        report.assessment_id != account.assessment.id
        or report.config_digest != account.assessment.config_digest
    ):
        raise ValueError("account_binding_mismatch")
    calls = account.calls()
    schedules = account.scheduled()
    uncertain = any(
        c.state != "completed"
        or c.usage is None
        or c.usage.cost_decimal is None
        for c in calls
    )
    uncertain = uncertain or any(
        code.startswith("budget_") for _, code in schedules
    )
    limitations = [*report.limitations, "synthetic_harness_not_native_proof"]
    if any(code == "comparator_unsupported" for _, code in schedules):
        limitations.append("unsupported_comparators_unmeasured")
    if uncertain:
        limitations.append("budget_refusal_or_uncertain_spend")
    identities = tuple(
        dict.fromkeys((*report.trial_ids, *(s.trial_id for s, _ in schedules)))
    )
    return report.model_copy(
        update={
            "status": "revise" if report.release_blockers else "unverified",
            "trial_ids": identities,
            "limitations": tuple(dict.fromkeys(limitations)),
        }
    )
