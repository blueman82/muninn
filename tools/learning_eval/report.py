"""Write strict offline reports with explicit null measurements."""

from __future__ import annotations

from pathlib import Path

from muninn.review_charge import aggregate
from muninn.review_claims import UNKNOWN_CONFIG
from muninn.review_json import canonical_json
from muninn.review_models import ObservedConfig
from muninn.review_runner import read_private_file
from tools.learning_eval import score
from tools.learning_eval.account import Account
from tools.learning_eval.cases import digest
from tools.learning_eval.models import (
    AssessmentStatus,
    Conditions,
    EvaluationReport,
    HostResult,
    MetricResult,
    Trial,
)
from tools.learning_eval.pilot_evidence import SavedOutput
from tools.learning_eval.pilot_models import (
    PilotManifest,
    PilotMaterial,
    PilotTrial,
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


def pilot_report(
    manifest: PilotManifest,
    material: PilotMaterial,
    results: tuple[PilotTrial, ...],
    account: Account,
) -> EvaluationReport:
    """Report saved outputs with separately bound judgments."""
    judgments = {j.trial_id: j for j in manifest.judgments}
    if len(judgments) != len(manifest.judgments) or not set(judgments) <= {
        r.id for r in results
    }:
        raise ValueError("pilot_judgment_binding")
    trials: list[Trial] = []
    policy = manifest.assessment.policies[0]
    requested = ObservedConfig.model_validate(
        policy.runner.model_dump(exclude={"executable"})
    )
    for result in results:
        judgment = judgments.get(result.id)
        if judgment is not None and (
            judgment.result_digest != digest(result.model_dump(mode="json"))
            or judgment.judgment.method != "human"
        ):
            raise ValueError("pilot_judgment_binding")
        observed = UNKNOWN_CONFIG
        for path in result.output_paths:
            saved = SavedOutput.model_validate_json(
                read_private_file(path, 4 * 1024 * 1024)
            )
            if saved.schedule.purpose == "task":
                observed = saved.observed
        source = next(
            s for s in material.sources if s.case_id == result.case_id
        )
        conditions = Conditions(
            evidence_available_digest=source.sha256,
            evidence_selected_digest=source.sha256,
            native_state="synthetic",
            cache_state="isolated",
            approval_mode="synthetic",
        )
        calls = [
            c for c in account.calls() if c.schedule.trial_id == result.id
        ]
        trials.append(
            Trial(
                id=result.id,
                case_id=result.case_id,
                comparator=result.comparator,
                pair_id=result.pair_id,
                learned_state_digest=(
                    result.exposure.learned_digest if result.exposure else None
                ),
                policy_digest=digest(policy.model_dump(mode="json")),
                budget_account_id=manifest.assessment.budget.account_id,
                expected_conditions=conditions,
                observed_conditions=(
                    conditions if result.outcome == "completed" else None
                ),
                requested=requested,
                observed=observed,
                outcome=result.outcome,
                usage=aggregate([c.charge() for c in calls]),
                timings_seconds={},
                resource_measurements={},
                judgment=judgment.judgment if judgment else None,
                evidence_paths=result.output_paths,
            )
        )
    return with_account(
        score.score_trials(material.cases, tuple(trials), manifest.assessment),
        account,
    )
