"""Require external frozen authority before reading synthetic material."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from muninn.review_models import ObservedConfig
from muninn.review_process_protocol import activation, decode_activation
from tests import test_learning_eval_capability as capability_tests
from tests.test_learning_eval_contracts import (
    FIXTURE,
    assessment,
    assessment_data,
    judgment,
    trial,
)
from tools.learning_eval import capability, execution
from tools.learning_eval.cases import (
    config_digest,
    digest,
    input_digests,
    load_cases,
)
from tools.learning_eval.models import Assessment
from tools.learning_eval.run import main, validate_manifest
from tools.learning_eval.score import comparable_pair, score_trials


class LearningGuardsTests(unittest.TestCase):
    """Exercise causal attribution and refusal before material access."""

    def manifest(self, phase: str = "heldout") -> bytes:
        """Build fabricated metadata from the checked-in synthetic fixture."""
        data = assessment_data()
        data.update(input_digests(load_cases(FIXTURE)))
        data.update(phase=phase, frozen_at=2.0)
        config = assessment(**data)
        config = config.model_copy(
            update={"config_digest": config_digest(config)}
        )
        return json.dumps(
            {
                "assessment": config.model_dump(mode="json"),
                "cases_path": str(FIXTURE),
                "trials": [],
                "host_results": [],
                "synthetic": True,
            }
        ).encode()

    def test_heldout_self_digest_is_not_read_authority(self) -> None:
        raw = self.manifest()
        with mock.patch("tools.learning_eval.run.load_cases") as read:
            with self.assertRaisesRegex(ValueError, "freeze_binding_required"):
                validate_manifest(raw, "heldout")
            read.assert_not_called()

    def test_config_mismatch_refuses_before_case_read(self) -> None:
        data = json.loads(self.manifest("pilot"))
        data["assessment"]["config_digest"] = "0" * 64
        with mock.patch("tools.learning_eval.run.load_cases") as read:
            with self.assertRaisesRegex(ValueError, "config_digest_mismatch"):
                validate_manifest(json.dumps(data).encode(), "pilot")
            read.assert_not_called()

    def test_historical_unknown_conditions_are_visibly_unmatched(self) -> None:
        case = load_cases(FIXTURE)[0]
        item = trial(
            case.id,
            judgment=judgment(0, 1),
            expected_conditions=None,
            observed_conditions=None,
        )
        report = score_trials((case,), (item,), assessment())
        self.assertIn(
            "unmatched_conditions_not_attributable_to_memory",
            report.limitations,
        )

    def test_external_freeze_binds_temporary_material_and_digests(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fabricated-heldout.json"
            fabricated = json.loads(FIXTURE.read_bytes())
            for item in fabricated["cases"]:
                item["partition"] = "heldout"
            path.write_text(json.dumps(fabricated))
            raw = json.loads(self.manifest())
            data = raw["assessment"]
            data.update(input_digests(load_cases(path)))
            data["sample_size"] = sum(
                c.partition == "heldout" for c in load_cases(path)
            )
            config = Assessment.model_validate_json(json.dumps(data))
            frozen = config.model_copy(
                update={"config_digest": config_digest(config)}
            )
            raw.update(
                assessment=frozen.model_dump(mode="json"), cases_path=str(path)
            )
            valid = json.dumps(raw).encode()
            with mock.patch(
                "tools.learning_eval.run.load_cases", wraps=load_cases
            ) as read:
                validate_manifest(valid, "heldout", frozen_assessment=frozen)
                read.assert_called_once_with(path)
            changed = frozen.model_copy(
                update={"approved_by": "different-synthetic-owner"}
            )
            with mock.patch("tools.learning_eval.run.load_cases") as read:
                with self.assertRaisesRegex(
                    ValueError, "freeze_binding_mismatch"
                ):
                    validate_manifest(
                        valid, "heldout", frozen_assessment=changed
                    )
                read.assert_not_called()
            material = json.loads(path.read_bytes())
            material["cases"][0]["expected"][
                "labeler_id"
            ] = "changed-synthetic-labeler"
            path.write_text(json.dumps(material))
            with self.assertRaisesRegex(ValueError, "input_digest_mismatch"):
                validate_manifest(valid, "heldout", frozen_assessment=frozen)

    def test_unknown_or_changed_conditions_prevent_causal_pairing(
        self,
    ) -> None:
        config = ObservedConfig(
            runner="claude_cli",
            version="synthetic",
            model="synthetic-model",
            reasoning="high",
        )
        label = judgment(0, 1).model_copy(update={"later_task_passed": True})
        left = trial(
            "synthetic", requested=config, observed=config, judgment=label
        )
        right = left.model_copy(
            update={"id": "right", "comparator": "new_withheld"}
        )
        self.assertTrue(comparable_pair(left, right))
        self.assertFalse(
            comparable_pair(
                left, right.model_copy(update={"expected_conditions": None})
            )
        )
        assert right.observed_conditions is not None
        changed = right.observed_conditions.model_copy(
            update={"cache_state": "warm"}
        )
        self.assertFalse(
            comparable_pair(
                left, right.model_copy(update={"observed_conditions": changed})
            )
        )

    def test_cli_scores_the_material_it_validated_without_rereading(
        self,
    ) -> None:
        raw = json.loads(self.manifest("pilot"))
        data = raw["assessment"]
        data["sample_size"] = len(load_cases(FIXTURE))
        config = Assessment.model_validate_json(json.dumps(data))
        data["config_digest"] = config_digest(config)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(raw))
            argv = [
                "learning_eval",
                "--manifest",
                str(path),
                "--phase",
                "pilot",
                "--output",
                directory,
            ]
            with (
                mock.patch("sys.argv", argv),
                redirect_stdout(io.StringIO()),
                mock.patch(
                    "tools.learning_eval.run.load_cases",
                    side_effect=[
                        load_cases(FIXTURE),
                        AssertionError("material reread after validation"),
                    ],
                ),
            ):
                try:
                    result = main()
                except AssertionError as exc:
                    self.fail(str(exc))
                self.assertEqual(result, 0)


class LearningExecutionFaultTests(unittest.TestCase):
    """Mutate real launch boundaries while every child remains synthetic."""

    def fixture(self) -> capability_tests.LearningCapabilityTests:
        """Retain disposable candidate files until the fault is reconciled."""
        fixture = capability_tests.LearningCapabilityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def mutate(
        self, fixture: capability_tests.LearningCapabilityTests, target: str
    ) -> None:
        """Alter one bound input at a controlled preparation boundary."""
        if target == "candidate":
            fixture.path.write_bytes(fixture.path.read_bytes() + b" ")
        elif target == "executable":
            fixture.fixture.executable.write_text(
                "changed synthetic executable"
            )
        elif target == "roots":
            fixture.fixture.settings.write_text("changed synthetic config")
        else:
            patch = (
                mock.patch.dict("os.environ", {"TEST_AUTH": "changed"})
                if target == "environment"
                else (
                    mock.patch.object(
                        capability, "round_schema", return_value={}
                    )
                    if target == "schema"
                    else mock.patch.object(
                        capability,
                        "output_contract_digest",
                        return_value="0" * 64,
                    )
                )
            )
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_bound_input_rechecked_before_create_and_activation(
        self,
    ) -> None:
        for stage in (2, 3):
            for target in (
                "candidate",
                "executable",
                "roots",
                "environment",
                "schema",
                "decoder",
            ):
                with self.subTest(stage=stage, target=target):
                    fixture = self.fixture()
                    account, schedule = fixture.execution_setup()
                    prepare = execution.prepare_capability_trial
                    count = 0

                    def drift(
                        *args: Any,
                        boundary: int = stage,
                        current: Any = fixture,
                        mutation: str = target,
                        original: Any = prepare,
                    ) -> Any:
                        nonlocal count
                        count += 1
                        if count == boundary:
                            self.mutate(current, mutation)
                        return original(*args)

                    with (
                        account,
                        mock.patch(
                            "time.monotonic",
                            side_effect=capability_tests.CLOCK,
                        ),
                        mock.patch.object(
                            execution,
                            "prepare_capability_trial",
                            side_effect=drift,
                        ),
                    ):
                        if stage == 2:
                            with mock.patch(
                                "muninn.review_spawn.subprocess.Popen"
                            ) as popen:
                                with self.assertRaises(ValueError):
                                    execution.execute_trial(
                                        account,
                                        fixture.context,
                                        fixture.capability,
                                        b"synthetic",
                                        fixture.auth,
                                        schedule,
                                    )
                                popen.assert_not_called()
                        else:
                            call = execution.execute_trial(
                                account,
                                fixture.context,
                                fixture.capability,
                                b"synthetic",
                                fixture.auth,
                                schedule,
                            )
                            self.assertFalse(call.activation_claimed)
                            self.assertTrue(call.cleaned)
                        self.assertEqual(len(account.calls()), 1)
                        self.assertEqual(
                            account.calls()[0].cost_reservation_decimal, "1"
                        )
                    self.doCleanups()

    def test_registration_and_ready_persistence_failure_contain_children(
        self,
    ) -> None:
        for target in ("registration", "persistence"):
            with self.subTest(target=target):
                fixture = self.fixture()
                account, schedule = fixture.execution_setup()
                patch = (
                    mock.patch.object(
                        execution.ProcessMonitor,
                        "register",
                        side_effect=ValueError("synthetic"),
                    )
                    if target == "registration"
                    else mock.patch.object(
                        account, "ready", side_effect=OSError("synthetic")
                    )
                )
                with (
                    account,
                    patch,
                    mock.patch(
                        "time.monotonic", side_effect=capability_tests.CLOCK
                    ),
                ):
                    call = execution.execute_trial(
                        account,
                        fixture.context,
                        fixture.capability,
                        b"synthetic",
                        fixture.auth,
                        schedule,
                    )
                    self.assertEqual(call.state, "unknown")
                    self.assertTrue(call.cleaned)
                    self.assertFalse(call.activation_claimed)
                    self.assertIsNone(call.usage)
                self.doCleanups()

    def test_output_and_deadline_failures_retain_charge_and_cleanup(
        self,
    ) -> None:
        for body in ("print('x' * 5000)", "import time; time.sleep(9)"):
            with self.subTest(body=body):
                fixture = self.fixture()
                account, schedule = fixture.execution_setup()
                fixture.fixture.executable.write_text(
                    f"#!{sys.executable}\nimport sys\n"
                    f"sys.stdin.buffer.read()\n{body}\n"
                )
                fixture.candidate["executable_sha256"] = hashlib.sha256(
                    fixture.fixture.executable.read_bytes()
                ).hexdigest()
                fixture.refresh()
                schedule = schedule.model_copy(
                    update={
                        "authorization_digest": digest(
                            fixture.auth.model_dump(mode="json")
                        )
                    }
                )
                context = fixture.context.model_copy(
                    update={"deadline": capability_tests.CLOCK() + 0.5}
                )
                with (
                    account,
                    mock.patch(
                        "time.monotonic", side_effect=capability_tests.CLOCK
                    ),
                ):
                    call = execution.execute_trial(
                        account,
                        context,
                        fixture.capability,
                        b"synthetic",
                        fixture.auth,
                        schedule,
                    )
                    self.assertEqual(call.state, "unknown")
                    self.assertTrue(call.cleaned)
                    self.assertIsNotNone(call.outcome)
                    self.assertEqual(call.cost_reservation_decimal, "1")
                self.doCleanups()

    def test_exact_prepared_recipe_survives_transport(self) -> None:
        spec = self.fixture().prepare()
        raw = activation(
            spec.cli_argv, spec.cli_cwd, spec.cli_env, spec.prompt, 8192
        )
        try:
            decoded = decode_activation(raw, 8192)
        except ValueError as exc:
            self.fail(f"fixed recipe refused: {exc}")
        self.assertEqual(
            decoded, (spec.cli_argv, spec.cli_cwd, spec.cli_env, spec.prompt)
        )
        index = spec.cli_argv.index("--tools")
        self.assertEqual(spec.cli_argv[index + 1], "")
