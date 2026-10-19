"""Exercise unverified candidates using private synthetic bytes only."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from muninn import review_runner
from muninn.review_models import ProcessIdentity, ReviewContext
from tests import test_review_runner as runner_tests
from tests.test_learning_eval_account import account_assessment
from tools.learning_eval import execution, report, run
from tools.learning_eval.account import Account
from tools.learning_eval.cases import config_digest, digest
from tools.learning_eval.models import TrialAuthorization
from tools.learning_eval.score import score_trials
from tools.learning_eval.trial_models import AccountCall, ScheduledCall

CLOCK = time.monotonic


class LearningCapabilityTests(unittest.TestCase):
    """Prove static isolation without issuing native host proof."""

    def setUp(self) -> None:
        self.fixture = runner_tests.ReviewRunnerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.context = self.fixture.context
        self.candidate = {
            key: value
            for key, value in self.fixture.proof.items()
            if key
            not in {
                "provenance",
                "cli_argv",
                "coverage",
                "disabled",
                "complete",
            }
        }
        self.candidate.update(
            kind="synthetic_candidate",
            assessment_id="synthetic",
            budget_account_id=self.context.policy.budget.account_id,
        )
        self.path = self.fixture.home / "candidate.json"
        self.refresh()

    def refresh(self) -> None:
        """Bind newly fabricated candidate bytes to explicit test authority."""
        raw = json.dumps(self.candidate).encode()
        self.path.write_bytes(raw)
        self.path.chmod(0o600)
        self.capability = self.fixture.capability.model_copy(
            update={
                "status": "unverified",
                "proof_path": self.path,
            }
        )
        self.auth = TrialAuthorization(
            assessment_id="synthetic",
            candidate_digest=hashlib.sha256(raw).hexdigest(),
            config_digest=self.capability.config_digest,
            budget=self.context.policy.budget,
            disposable_home=self.fixture.home,
            approved_by="synthetic-owner",
            approved_at=1.0,
        )

    def prepare(self) -> review_runner.LaunchSpec:
        """Exercise the exact callable harness seam."""
        return run.prepare_capability_trial(
            self.context, self.capability, b"synthetic", self.auth
        )

    def test_unverified_candidate_fixed_recipe_production_stays_closed(
        self,
    ) -> None:
        spec = self.prepare()
        self.assertEqual(spec.cli_argv, tuple(self.fixture.argv))
        self.assertEqual(set(spec.cli_env), {"HOME", "TEST_AUTH"})
        with self.assertRaisesRegex(ValueError, "capability_unverified"):
            review_runner.prepare_launch(
                self.context, self.capability, b"synthetic"
            )
        self.assertFalse((self.fixture.home / "account.sqlite3").exists())

    def test_candidate_and_configuration_drift_refuse(self) -> None:
        self.path.write_bytes(self.path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "capability_changed"):
            self.prepare()
        self.refresh()
        self.fixture.settings.write_text("changed")
        with self.assertRaisesRegex(ValueError, "capability_changed"):
            self.prepare()

    def test_executable_environment_schema_decoder_drift_refuse(self) -> None:
        for target in ("schema", "decoder", "environment", "executable"):
            with self.subTest(target=target):
                module = importlib.import_module(
                    "tools.learning_eval.capability"
                )
                if target == "schema":
                    patch = mock.patch.object(
                        module, "round_schema", return_value={}
                    )
                elif target == "decoder":
                    patch = mock.patch.object(
                        module, "output_contract_digest", return_value="0" * 64
                    )
                elif target == "environment":
                    patch = mock.patch.dict(
                        "os.environ", {"TEST_AUTH": "changed"}
                    )
                else:
                    self.fixture.executable.write_text("changed")
                    patch = mock.patch.dict("os.environ", {})
                with (
                    patch,
                    self.assertRaisesRegex(ValueError, "capability_changed"),
                ):
                    self.prepare()

    def test_wrong_budget_home_round_and_prompt_refuse(self) -> None:
        for update in (
            {
                "budget": self.auth.budget.model_copy(
                    update={"account_id": "other"}
                )
            },
            {"disposable_home": self.fixture.cwd},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                run.prepare_capability_trial(
                    self.context,
                    self.capability,
                    b"x",
                    self.auth.model_copy(update=update),
                )
        with self.assertRaisesRegex(ValueError, "invalid_prompt_size"):
            run.prepare_capability_trial(
                self.context,
                self.capability,
                b"x" * (self.context.policy.max_input_bytes + 1),
                self.auth,
            )
        invalid = self.context.model_copy(
            update={"round_index": self.context.policy.max_rounds + 1}
        )
        with self.assertRaisesRegex(ValueError, "budget_round_limit"):
            run.prepare_capability_trial(
                invalid, self.capability, b"x", self.auth
            )

    def execution_setup(self) -> tuple[Account, ScheduledCall]:
        """Prepare a deterministic child and synthetic account metadata."""
        config = account_assessment()
        policy = self.context.policy.model_copy(
            update={"budget": config.budget, "deadline_seconds": 5.0}
        )
        self.context = self.context.model_copy(
            update={"policy": policy, "deadline": CLOCK() + 5.0}
        )
        self.candidate["budget_account_id"] = config.budget.account_id
        program = (
            "#!"
            + sys.executable
            + "\nimport json, sys\nsys.stdin.buffer.read()\n"
            'print(json.dumps({"type":"result","subtype":"success",'
            '"is_error":False,"structured_output":{}}))\n'
        )
        self.fixture.executable.write_text(program)
        self.fixture.executable.chmod(0o700)
        self.candidate["executable_sha256"] = hashlib.sha256(
            self.fixture.executable.read_bytes()
        ).hexdigest()
        self.refresh()
        observed = {
            k: v
            for k, v in policy.runner.model_dump().items()
            if k != "executable"
        }
        config = config.model_copy(
            update={
                "policies": (policy,),
                "comparator_config_digests": dict.fromkeys(
                    config.comparators, digest(observed)
                ),
            }
        )
        config = config.model_copy(
            update={"config_digest": config_digest(config)}
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        call = ScheduledCall(
            id=self.context.attempt_id,
            assessment_id=config.id,
            comparator="new_present",
            trial_id="synthetic",
            retry_index=1,
            round_index=1,
            policy_digest=digest(policy.model_dump(mode="json")),
            config_digest=config.config_digest,
            authorization_digest=digest(self.auth.model_dump(mode="json")),
        )
        return Account(root, config, initialize=True), call

    def execute(
        self,
        account: Account,
        schedule: ScheduledCall,
        context: ReviewContext | None = None,
    ) -> AccountCall:
        """Execute one bound synthetic request using the fixture authority."""
        return execution.execute_trial(
            account,
            context or self.context,
            self.capability,
            b"synthetic",
            self.auth,
            schedule,
        )

    def test_missing_and_unreadable_inputs_retain_refused_schedules(
        self,
    ) -> None:
        for target in ("candidate", "executable"):
            for failure in ("missing", "unreadable"):
                with self.subTest(target=target, failure=failure):
                    account, schedule = self.execution_setup()
                    path = (
                        self.path
                        if target == "candidate"
                        else self.fixture.executable
                    )
                    if failure == "missing":
                        path.unlink()
                    # Privileged hosts can read mode 000, so inject the fault.
                    with (
                        account,
                        mock.patch(
                            "muninn.review_spawn.subprocess.Popen"
                        ) as popen,
                        (
                            mock.patch(
                                "tools.learning_eval.capability."
                                + (
                                    "read_private_file"
                                    if target == "candidate"
                                    else "file_digest"
                                ),
                                side_effect=PermissionError(
                                    "synthetic-private-detail"
                                ),
                            )
                            if failure == "unreadable"
                            else nullcontext()
                        ),
                    ):
                        with self.assertRaises(OSError):
                            self.execute(account, schedule)
                        self.assertEqual(
                            account.scheduled(),
                            ((schedule, "configuration_refused"),),
                        )
                        self.assertEqual(account.calls(), ())
                        popen.assert_not_called()
                    with Account(account.path, account.assessment) as reopened:
                        self.assertEqual(
                            reopened.scheduled(),
                            ((schedule, "configuration_refused"),),
                        )
                        result = report.with_account(
                            score_trials((), (), account.assessment), reopened
                        )
                        self.assertEqual(
                            result.trial_ids, (schedule.trial_id,)
                        )
                        self.assertEqual(result.status, "unverified")

    def test_execution_claim_commit_identity_and_duplicate(self) -> None:
        account, schedule = self.execution_setup()
        spawn = importlib.import_module("muninn.review_spawn")
        native = spawn.subprocess.Popen

        def inspect_spawn(*args: object, **kwargs: object) -> object:
            records = account.calls()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].state, "reserved")
            return native(*args, **kwargs)

        with (
            account,
            mock.patch("time.monotonic", side_effect=CLOCK),
            mock.patch.object(
                spawn.subprocess, "Popen", side_effect=inspect_spawn
            ) as popen,
        ):
            call = self.execute(account, schedule)
            self.assertEqual(call.state, "completed", repr(call))
            self.assertTrue(call.activation_claimed)
            self.assertTrue(call.cleaned)
            self.assertIsNotNone(call.process_identity)
            again = self.execute(account, schedule)
            self.assertEqual(call, again)
            self.assertEqual(popen.call_count, 1)
            with self.assertRaisesRegex(ValueError, "invalid_call_phase"):
                account.activate(call.schedule.id, time.time())

    def test_ready_drift_refuses_activation_and_retains_charge(self) -> None:
        account, schedule = self.execution_setup()
        ready = account.ready

        def mutate(
            identity: str, process: ProcessIdentity, now: float
        ) -> None:
            ready(identity, process, now)
            self.fixture.settings.write_text(
                "changed between ready and activation"
            )

        with (
            account,
            mock.patch("time.monotonic", side_effect=CLOCK),
            mock.patch.object(account, "ready", side_effect=mutate),
        ):
            call = self.execute(account, schedule)
            self.assertEqual(call.state, "unknown")
            self.assertFalse(call.activation_claimed)
            self.assertTrue(call.cleaned)
            self.assertEqual(call.cost_reservation_decimal, "1")

    def test_cap_refusal_starts_zero_wrappers(self) -> None:
        account, schedule = self.execution_setup()
        with account:
            first, _ = account.claim(schedule, self.auth, 10.0)
            account.finish(first.schedule.id, None, "synthetic_refusal", 10.0)
            another = schedule.model_copy(
                update={"id": "A_" + "f" * 32, "trial_id": "another"}
            )
            with self.assertRaisesRegex(ValueError, "budget_wall_limit"):
                account.claim(another, self.auth, 110.0)
        with (
            Account(account.path, account.assessment) as reopened,
            mock.patch("muninn.review_spawn.subprocess.Popen") as popen,
        ):
            for now, code in (
                (109.0, "budget_clock_reversed"),
                (110.0, "budget_wall_limit"),
            ):
                with (
                    mock.patch("time.time", return_value=now),
                    self.assertRaisesRegex(ValueError, code),
                ):
                    self.execute(reopened, another)
                popen.assert_not_called()
                self.assertEqual(len(reopened.calls()), 1)

    def test_unsupported_comparators_remain_scheduled_without_spawn(
        self,
    ) -> None:
        account, schedule = self.execution_setup()
        with (
            account,
            mock.patch("muninn.review_spawn.subprocess.Popen") as popen,
        ):
            for label in ("hermes", "current_muninn"):
                item = schedule.model_copy(
                    update={
                        "id": "A_" + ("b" if label == "hermes" else "c") * 32,
                        "comparator": label,
                    }
                )
                with self.assertRaisesRegex(
                    ValueError, "comparator_unsupported"
                ):
                    self.execute(account, item)
            self.assertEqual(len(account.scheduled()), 2)
            self.assertTrue(
                all(
                    code == "comparator_unsupported"
                    for _, code in account.scheduled()
                )
            )
            self.assertEqual(account.calls(), ())
            popen.assert_not_called()

    def test_account_wall_ceiling_bounds_activation(self) -> None:
        config = account_assessment(max_wall_seconds=0.01)
        with mock.patch(f"{__name__}.account_assessment", return_value=config):
            account, schedule = self.execution_setup()
        with account, mock.patch("time.monotonic", side_effect=CLOCK):
            try:
                self.execute(account, schedule)
            except ValueError as exc:
                self.assertEqual(str(exc), "budget_deadline")
            self.assertFalse(
                account.calls()[0].activation_claimed,
                "activation outlived the account wall ceiling",
            )

    def test_each_round_has_its_own_call_without_changing_review_attempt(
        self,
    ) -> None:
        account, schedule = self.execution_setup()
        with account, mock.patch("time.monotonic", side_effect=CLOCK):
            self.execute(account, schedule)
            second = schedule.model_copy(
                update={"id": "A_" + "d" * 32, "round_index": 2}
            )
            context = self.context.model_copy(update={"round_index": 2})
            result = self.execute(account, second, context)
            self.assertEqual(result.state, "completed")
            self.assertEqual(len(account.calls()), 2)
