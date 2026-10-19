"""Exercise frozen identities and concrete synthetic fault labels."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from muninn.review_models import ObservedConfig, ReviewPolicy, Usage
from tools.learning_eval.cases import (
    digest,
    input_digests,
    load_cases,
    required_host_seed,
)
from tools.learning_eval.models import (
    Assessment,
    Conditions,
    HostResult,
    IndependentJudgment,
    Trial,
)
from tools.learning_eval.score import score_trials

FIXTURE = Path(__file__).parent / "fixtures/learning/cases.json"
DIGEST = "a" * 64


def policy_data() -> dict[str, Any]:
    """Supply all frozen policy fields using synthetic authorization only."""
    return {
        "schema_version": 1,
        "policy_id": "synthetic",
        "prompt_digest": DIGEST,
        "projection_version": "synthetic-1",
        "result_schema_digest": DIGEST,
        "runner": {
            "runner": "claude_cli",
            "executable": "synthetic-not-executed",
            "version": "1",
            "model": "saving-model",
            "reasoning": "explicit",
        },
        "max_rounds": 1,
        "max_input_bytes": 1024,
        "max_output_bytes": 1024,
        "context_bytes": 1024,
        "deadline_seconds": 1.0,
        "max_attempts": 1,
        "retry_backoff_seconds": [],
        "concurrency": 1,
        "hook_wait_seconds": 0.0,
        "allow_global": False,
        "budget": {
            "account_id": "synthetic",
            "max_model_calls": 1,
            "max_wall_seconds": 1.0,
            "max_cost_decimal": None,
            "conservative_call_cost_decimal": None,
            "currency": None,
            "approved_by": "synthetic-owner",
            "approved_at": 1.0,
        },
    }


def requested_digest() -> str:
    """Return the explicit unknown comparator settings identity."""
    return digest(
        {"runner": None, "version": None, "model": None, "reasoning": None}
    )


def assessment_data() -> dict[str, Any]:
    """Return explicit synthetic settings, never owner authorization."""
    return {
        "schema_version": 1,
        "id": "synthetic",
        "phase": "pilot",
        "required_hosts": [
            host.model_dump(mode="json") for host in required_host_seed()
        ],
        "policies": [policy_data()],
        "budget": {
            "account_id": "synthetic",
            "max_model_calls": 1,
            "max_wall_seconds": 1.0,
            "max_cost_decimal": None,
            "conservative_call_cost_decimal": None,
            "currency": None,
            "approved_by": "synthetic-owner",
            "approved_at": 1.0,
        },
        "criteria": [
            {
                "metric": "unsupported_saves",
                "unit": "proportion",
                "direction": "at_most",
                "threshold": 0.1,
                "interval_method": "exact_binomial",
                "confidence": 0.95,
            }
        ],
        "comparators": ["new_present"],
        "comparator_config_digests": {"new_present": requested_digest()},
        "sample_size": 1,
        "repetitions": 1,
        "bootstrap_repetitions": 100,
        "seed": 19,
        "grader_ids": ["synthetic-rule"],
        "labels_digest": DIGEST,
        "cases_digest": DIGEST,
        "heldout_family_digest": DIGEST,
        "config_digest": DIGEST,
        "frozen_at": None,
        "approved_by": "synthetic-owner",
        "approved_at": 1.0,
    }


def assessment(**changes: Any) -> Assessment:
    """Validate an explicit synthetic assessment with selected variations."""
    data = assessment_data() | changes
    return Assessment.model_validate_json(json.dumps(data))


def trial(case_id: str, **changes: Any) -> Trial:
    """Return an unmeasured synthetic attempt with explicit unknowns."""
    config = ObservedConfig(
        runner=None, version=None, model=None, reasoning=None
    )
    conditions = Conditions(
        evidence_available_digest=DIGEST,
        evidence_selected_digest=DIGEST,
        native_state="synthetic-none",
        cache_state="cold",
        approval_mode="synthetic",
    )
    data = {
        "expected_conditions": conditions,
        "observed_conditions": conditions,
        "id": "attempt-1",
        "case_id": case_id,
        "comparator": "new_present",
        "policy_digest": digest(
            ReviewPolicy.model_validate_json(
                json.dumps(policy_data())
            ).model_dump(mode="json")
        ),
        "budget_account_id": "synthetic",
        "requested": config,
        "observed": config,
        "outcome": "interrupted",
        "usage": Usage(
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            cost_decimal=None,
            currency=None,
        ),
        "timings_seconds": {},
        "resource_measurements": {},
        "judgment": None,
        "evidence_paths": (),
    } | changes
    return Trial.model_validate(data)


def judgment(numerator: int, denominator: int) -> IndependentJudgment:
    """Provide a separately attributed frozen synthetic rule result."""
    return IndependentJudgment(
        grader_id="synthetic-rule",
        method="frozen_rule",
        grader_version="1",
        evidence_paths=(FIXTURE,),
        numerators={"unsupported_saves": numerator},
        denominators={"unsupported_saves": denominator},
        later_task_passed=None,
    )


def accepted_hosts(config: Assessment) -> tuple[HostResult, ...]:
    """Return synthetic native proofs for aggregate regression only."""
    return tuple(
        HostResult(
            requirement=h,
            os_version="synthetic-1",
            host_version="synthetic-1",
            provider_version="synthetic-1",
            service_version="synthetic-1",
            trust_identity="synthetic",
            native_events=h.required_events,
            recovered_events=(),
            status="accept",
            failure_codes=(),
            evidence_paths=(FIXTURE,),
        )
        for h in config.required_hosts
    )


class LearningEvalContractTests(unittest.TestCase):
    """Catch trace rebinding, lost uncertainty and inapplicable controls."""

    def test_trial_policy_and_requested_configuration_are_frozen(self) -> None:
        case = load_cases(FIXTURE)[0]
        config = assessment()
        valid = trial(case.id, judgment=judgment(0, 100))
        self.assertEqual(
            score_trials(
                (case,), (valid,), config, host_results=accepted_hosts(config)
            ).status,
            "accept",
        )
        for attempt, settings in (
            (valid.model_copy(update={"policy_digest": "b" * 64}), config),
            (valid, config.model_copy(update={"policies": ()})),
            (
                valid,
                config.model_copy(
                    update={
                        "policies": (
                            config.policies[0].model_copy(
                                update={"deadline_seconds": 2.0}
                            ),
                        )
                    }
                ),
            ),
            (
                valid.model_copy(
                    update={
                        "requested": valid.requested.model_copy(
                            update={"model": "unfrozen-model"}
                        )
                    }
                ),
                config,
            ),
        ):
            with (
                self.subTest(
                    attempt=attempt.policy_digest, settings=settings.id
                ),
                self.assertRaisesRegex(
                    ValueError, "unfrozen_(policy|configuration)"
                ),
            ):
                score_trials((case,), (attempt,), settings)
        changed = valid.model_copy(
            update={
                "observed": valid.observed.model_copy(
                    update={"model": "reported-other-model"}
                )
            }
        )
        report = score_trials(
            (case,), (changed,), config, host_results=accepted_hosts(config)
        )
        self.assertEqual(report.status, "inconclusive")
        self.assertIn(
            "unmatched_configuration_not_attributable_to_memory",
            report.limitations,
        )
        self.assertEqual(report.trial_ids, (valid.id,))

    def test_uncertainty_declaration_cannot_be_changed_silently(self) -> None:
        original = json.loads(FIXTURE.read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            original["uncertain_case_ids"].append(original["cases"][0]["id"])
            path.write_text(json.dumps(original))
            with self.assertRaisesRegex(ValueError, "uncertainty_mismatch"):
                load_cases(path)
            original["cases"][0]["expected"]["eligible_claim_keys"] = []
            path.write_text(json.dumps(original))
            changed = load_cases(path)
            self.assertNotEqual(
                input_digests(changed)["labels_digest"],
                input_digests(load_cases(FIXTURE))["labels_digest"],
            )
            result = score_trials(
                (changed[0],),
                (trial(changed[0].id, judgment=judgment(0, 100)),),
                assessment(),
            )
            self.assertIsNone(result.metrics[0].value)

    def test_paired_control_does_not_block_complete_treatment(self) -> None:
        case = load_cases(FIXTURE)[0]
        criterion = {
            "metric": "later_task",
            "unit": "paired_difference",
            "direction": "at_least",
            "threshold": 0.0,
            "interval_method": "family_bootstrap",
            "confidence": 0.95,
        }
        settings = ObservedConfig(
            runner="synthetic",
            model="saving-model",
            version="1",
            reasoning="explicit",
        )
        config = assessment(
            criteria=[criterion],
            comparators=["new_present", "new_withheld"],
            comparator_config_digests={
                k: digest(settings.model_dump(mode="json"))
                for k in ("new_present", "new_withheld")
            },
        )
        passed = judgment(0, 0).model_copy(
            update={
                "numerators": {},
                "denominators": {},
                "later_task_passed": True,
            }
        )
        failed = passed.model_copy(update={"later_task_passed": False})
        attempts = (
            trial(
                case.id, requested=settings, observed=settings, judgment=passed
            ),
            trial(
                case.id,
                id="control",
                comparator="new_withheld",
                requested=settings,
                observed=settings,
                judgment=failed,
            ),
        )
        report = score_trials(
            (case,), attempts, config, host_results=accepted_hosts(config)
        )
        self.assertEqual(report.status, "accept")
        self.assertEqual(
            [(m.comparator, m.value) for m in report.metrics],
            [("new_present", 1.0)],
        )
        self.assertEqual(report.trial_ids, ("attempt-1", "control"))
        missing = score_trials(
            (case,), attempts[:1], config, host_results=accepted_hosts(config)
        )
        self.assertEqual(missing.status, "inconclusive")

    def test_fault_fixtures_have_independent_state_outcomes(self) -> None:
        cases = {case.id: case for case in load_cases(FIXTURE)}
        required = {
            "recovery-stop_after_acknowledgment": {
                "request_state": "pending",
                "acknowledged": True,
            },
            "workers-duplicate_trigger": {
                "request_count": 1,
                "receipt_count": 1,
            },
            "erasure-partial_guide_support": {
                "erased_claim_visible": False,
                "surviving_claim_visible": True,
            },
            "schema_1_cutover-interrupted_copy": {
                "active_format": "legacy",
                "partial_copy_active": False,
            },
            "new_store_rebuild-fence_workers": {
                "stale_worker_write_count": 0,
                "active_generation": 2,
            },
            "cursor_context-resume_gap": {
                "native_resume_injection": False,
                "required_native_gap": True,
            },
            "failed_hook_persistence-indexed_recovery_not_native": {
                "native_completion_count": 0,
                "recovered_window_count": 1,
            },
        }
        for identity, expected in required.items():
            with self.subTest(identity=identity):
                case = cases[identity]
                self.assertIsNotNone(case.expected.later_task_success_rule)
                rule = json.loads(
                    case.expected.later_task_success_rule or "{}"
                )
                self.assertEqual(rule["equals"], expected)
                states = [
                    json.loads(record.excerpt)
                    for record in case.evidence
                    if record.excerpt
                ]
                self.assertTrue(all(state["synthetic"] for state in states))
                self.assertTrue(
                    any("input_state" in state for state in states)
                )
        partial = cases["erasure-partial_guide_support"]
        self.assertEqual(
            {r.state for r in partial.evidence}, {"live", "erased"}
        )
        for case in cases.values():
            if case.family_id in {
                "recovery",
                "workers",
                "erasure",
                "schema_1_cutover",
                "new_store_rebuild",
                "cursor_context",
            }:
                self.assertIsNotNone(case.expected.later_task_success_rule)
