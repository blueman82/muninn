"""Exercise offline assessment boundaries with synthetic evidence only."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from muninn.review_models import ObservedConfig
from tests.test_learning_eval_contracts import (
    FIXTURE,
    accepted_hosts,
    assessment,
    assessment_data,
    judgment,
    requested_digest,
    trial,
)
from tools.learning_eval.cases import (
    config_digest,
    digest,
    input_digests,
    load_cases,
    validate_partition,
)
from tools.learning_eval.models import (
    Assessment,
    HostResult,
)
from tools.learning_eval.report import write_report
from tools.learning_eval.run import validate_manifest
from tools.learning_eval.score import (
    binomial_interval,
    family_interval,
    score_trials,
)


class LearningEvalTests(unittest.TestCase):
    """Catch fabricated measurements, leakage and missing prerequisites."""

    def test_family_and_chronology_leakage_refused(self) -> None:
        cases = load_cases(FIXTURE)
        first = cases[0]
        for second in (
            first.model_copy(update={"id": "copy", "partition": "heldout"}),
            first.model_copy(
                update={"id": "copy", "chronology": first.chronology - 1}
            ),
        ):
            with self.assertRaisesRegex(ValueError, "partition_leakage"):
                validate_partition((first, second))

    def test_zero_denominator_and_missing_usage_stay_null(self) -> None:
        case = load_cases(FIXTURE)[0]
        attempt = trial(case.id, judgment=judgment(0, 0))
        report = score_trials((case,), (attempt,), assessment())
        metric = report.metrics[0]
        self.assertIsNone(metric.value)
        self.assertIsNone(metric.interval_low)
        self.assertEqual(metric.status, "inconclusive")
        self.assertIsNone(attempt.usage.input_tokens)
        self.assertIsNone(attempt.usage.cost_decimal)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_report(report, path)
            self.assertIsNone(
                json.loads(path.read_text())["metrics"][0]["value"]
            )

    def test_interrupted_trials_remain_in_denominator(self) -> None:
        case = load_cases(FIXTURE)[0]
        attempts = (
            trial(case.id, judgment=judgment(1, 2)),
            trial(
                case.id,
                id="attempt-2",
                outcome="unavailable",
                judgment=judgment(0, 3),
            ),
        )
        report = score_trials((case,), attempts, assessment(repetitions=2))
        self.assertEqual(report.metrics[0].denominator, 5)
        self.assertEqual(report.metrics[0].numerator, 1)
        self.assertEqual(report.metrics[0].sample_count, 2)
        self.assertEqual(report.trial_ids, ("attempt-1", "attempt-2"))
        missing = score_trials(
            (case,), attempts[:1], assessment(repetitions=2)
        )
        self.assertEqual(missing.metrics[0].sample_count, 2)
        self.assertEqual(missing.metrics[0].status, "inconclusive")

    def test_required_host_failure_cannot_be_dropped(self) -> None:
        config = assessment()
        failed = HostResult(
            requirement=config.required_hosts[0],
            os_version=None,
            host_version=None,
            provider_version=None,
            service_version=None,
            trust_identity=None,
            native_events=(),
            recovered_events=("completion",),
            status="revise",
            failure_codes=("native_event_missing",),
            evidence_paths=(),
        )
        report = score_trials((), (), config, host_results=(failed,))
        self.assertEqual(len(report.host_results), 12)
        self.assertEqual(report.host_results[0], failed)
        self.assertEqual(report.host_results[1].status, "unverified")
        self.assertNotEqual(report.status, "accept")
        remapped = failed.model_copy(
            update={
                "requirement": failed.requirement.model_copy(
                    update={"architecture": "invented"}
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "host_requirement_mismatch"):
            score_trials((), (), config, host_results=(remapped,))

    def test_paid_phase_requires_owner_budget(self) -> None:
        data = assessment_data()
        del data["budget"]
        raw = {
            "assessment": data,
            "cases_path": str(FIXTURE),
            "trials": [],
            "host_results": [],
            "synthetic": True,
        }
        with self.assertRaises(ValueError):
            validate_manifest(json.dumps(raw).encode(), "pilot")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(raw))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tools.learning_eval.run",
                    "--manifest",
                    str(manifest),
                    "--phase",
                    "pilot",
                    "--output",
                    str(root / "out"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "out").exists())

    def test_strict_counts_freeze_and_grader_independence(self) -> None:
        with self.assertRaises(ValidationError):
            judgment(2, 1)
        with self.assertRaises(ValidationError):
            assessment(phase="heldout")
        with self.assertRaises(ValidationError):
            assessment(sample_size=True)
        case = load_cases(FIXTURE)[0]
        config = ObservedConfig(
            runner="synthetic",
            version="1",
            model="synthetic-rule",
            reasoning=None,
        )
        attempt = trial(case.id, requested=config, judgment=judgment(0, 1))
        with self.assertRaisesRegex(ValueError, "grader_not_independent"):
            score_trials(
                (case,),
                (attempt,),
                assessment(
                    comparator_config_digests={
                        "new_present": digest(config.model_dump(mode="json"))
                    }
                ),
            )

    def test_release_blockers_override_scores(self) -> None:
        case = load_cases(FIXTURE)[0]
        report = score_trials(
            (case,),
            (trial(case.id, outcome="erasure_resurrection"),),
            assessment(),
        )
        self.assertEqual(report.status, "revise")
        self.assertEqual(report.release_blockers, ("erasure_resurrection",))

    def test_uncertain_labels_never_become_correctness(self) -> None:
        case = next(
            c for c in load_cases(FIXTURE) if c.id.endswith("uncertain_label")
        )
        report = score_trials(
            (case,), (trial(case.id, judgment=judgment(0, 100)),), assessment()
        )
        self.assertIsNone(report.metrics[0].value)
        self.assertEqual(report.metrics[0].status, "inconclusive")

    def test_synthetic_manifest_digests_and_cli_bundle(self) -> None:
        from_data = assessment_data()
        cases = load_cases(FIXTURE)
        from_data.update(input_digests(cases))
        from_data["sample_size"] = len(cases)
        config = Assessment.model_validate_json(json.dumps(from_data))
        from_data["config_digest"] = config_digest(config)
        raw = {
            "assessment": from_data,
            "cases_path": str(FIXTURE),
            "trials": [],
            "host_results": [],
            "synthetic": True,
        }
        validate_manifest(json.dumps(raw).encode(), "pilot")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "manifest.json"
            path.write_text(json.dumps(raw))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tools.learning_eval.run",
                    "--manifest",
                    str(path),
                    "--phase",
                    "pilot",
                    "--output",
                    str(root / "out"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            self.assertEqual(json.loads(result.stdout)["runner_calls"], 0)
            self.assertEqual(
                json.loads((root / "out/report.json").read_text())["status"],
                "unverified",
            )
            self.assertTrue((root / "out/trials.json").exists())
        from_data["labels_digest"] = "b" * 64
        from_data["config_digest"] = config_digest(
            Assessment.model_validate_json(json.dumps(from_data))
        )
        with self.assertRaisesRegex(ValueError, "input_digest_mismatch"):
            validate_manifest(json.dumps(raw).encode(), "pilot")

    def test_intervals_match_hand_calculated_bounds(self) -> None:
        low, high = binomial_interval(0, 1, 0.95)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.975)
        low, high = binomial_interval(1, 1, 0.95)
        self.assertAlmostEqual(low, 0.025)
        self.assertEqual(high, 1.0)
        self.assertEqual(
            family_interval({"a": [2.0], "b": [2.0]}, assessment(), 0.95),
            (2.0, 2.0),
        )

    def test_unknown_usage_metric_does_not_become_zero(self) -> None:
        case = load_cases(FIXTURE)[0]
        criteria = [
            {
                "metric": "input_tokens",
                "unit": "tokens",
                "direction": "at_most",
                "threshold": 1.0,
                "interval_method": "none",
                "confidence": None,
            }
        ]
        report = score_trials(
            (case,), (trial(case.id),), assessment(criteria=criteria)
        )
        self.assertIsNone(report.metrics[0].value)
        self.assertEqual(report.metrics[0].status, "inconclusive")

    def test_paired_outcomes_require_matched_reported_settings(self) -> None:
        case = load_cases(FIXTURE)[0]
        criterion = {
            "metric": "later_task",
            "unit": "paired_difference",
            "direction": "at_least",
            "threshold": 0.0,
            "interval_method": "family_bootstrap",
            "confidence": 0.95,
        }
        config = assessment(
            criteria=[criterion],
            comparators=["new_present", "new_withheld"],
            comparator_config_digests={
                "new_present": requested_digest(),
                "new_withheld": requested_digest(),
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
            trial(case.id, judgment=passed),
            trial(
                case.id,
                id="control",
                comparator="new_withheld",
                judgment=failed,
            ),
        )
        report = score_trials((case,), attempts, config)
        self.assertIsNone(report.metrics[0].value)
        settings = ObservedConfig(
            runner="synthetic",
            model="saving-model",
            version="1",
            reasoning="explicit",
        )
        matched = tuple(
            t.model_copy(update={"requested": settings, "observed": settings})
            for t in attempts
        )
        config = config.model_copy(
            update={
                "comparator_config_digests": {
                    key: digest(settings.model_dump(mode="json"))
                    for key in ("new_present", "new_withheld")
                }
            }
        )
        report = score_trials((case,), matched, config)
        self.assertEqual(report.metrics[0].value, 1.0)
        self.assertEqual(len(report.metrics), 1)

    def test_uncertain_save_labels_preserve_independent_retrieval(
        self,
    ) -> None:
        case = next(
            c for c in load_cases(FIXTURE) if c.id.endswith("uncertain_label")
        )
        expected = case.expected.model_copy(
            update={"retrieval_claim_keys": ("known-retrieval",)}
        )
        case = case.model_copy(update={"expected": expected})
        criterion = {
            "metric": "retrieval_useful",
            "unit": "proportion",
            "direction": "equal",
            "threshold": 1.0,
            "interval_method": "none",
            "confidence": None,
        }
        independent = judgment(0, 0).model_copy(
            update={
                "numerators": {"retrieval_useful": 1},
                "denominators": {"retrieval_useful": 1},
            }
        )
        report = score_trials(
            (case,),
            (trial(case.id, judgment=independent),),
            assessment(criteria=[criterion]),
        )
        self.assertEqual(report.metrics[0].value, 1.0)
        self.assertEqual(report.metrics[0].status, "accept")

    def test_unknown_metric_semantics_cannot_bypass_uncertainty(self) -> None:
        case = next(
            c for c in load_cases(FIXTURE) if c.id.endswith("uncertain_label")
        )
        settings = assessment()
        for name, unit in (
            ("missed_eligible_lessons", "proportion"),
            ("unsupported_saves", "seconds"),
            ("retrieval_useful", "seconds"),
            ("later_task", "proportion"),
        ):
            criterion = settings.criteria[0].model_dump(mode="json")
            criterion.update(
                metric=name, unit=unit, interval_method="none", confidence=None
            )
            with (
                self.subTest(name=name, unit=unit),
                self.assertRaisesRegex(ValueError, "unsupported_metric"),
            ):
                assessment(criteria=[criterion])
        criterion = settings.criteria[0].model_copy(
            update={"metric": "missed_eligible_lessons"}
        )
        bypassed = settings.model_copy(update={"criteria": (criterion,)})
        independent = judgment(0, 100).model_copy(
            update={
                "numerators": {"missed_eligible_lessons": 0},
                "denominators": {"missed_eligible_lessons": 100},
            }
        )
        with self.assertRaisesRegex(ValueError, "unsupported_metric"):
            score_trials(
                (case,),
                (trial(case.id, judgment=independent),),
                bypassed,
                host_results=accepted_hosts(bypassed),
            )
