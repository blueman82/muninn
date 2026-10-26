"""Keep explicit subscription account entry free of recovery signals."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from muninn.review_process_identity import identify
from tests import test_review_worker as worker_tests
from tests.test_learning_eval_account import account_assessment
from tools.learning_eval.account import Account
from tools.learning_eval.cases import digest
from tools.learning_eval.models import TrialAuthorization
from tools.learning_eval.trial_models import ScheduledCall


@unittest.skipIf(sys.platform == "win32", "POSIX harness account storage")
class SubscriptionAccountTests(unittest.TestCase):
    """Require durable refusal without changing owned-child liveness."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "account"
        self.path.mkdir(mode=0o700)
        self.home = self.root / "disposable"
        self.home.mkdir(mode=0o700)
        self.config = account_assessment(
            max_model_calls=2,
            max_wall_seconds=600.0,
            max_cost_decimal=None,
            conservative_call_cost_decimal=None,
            currency=None,
        )
        self.authorization = TrialAuthorization(
            assessment_id=self.config.id,
            candidate_digest="a" * 64,
            config_digest=self.config.config_digest,
            budget=self.config.budget,
            disposable_home=self.home,
            approved_by=self.config.approved_by,
            approved_at=self.config.approved_at,
        )

    def strict(self, *, initialize: bool = False) -> Account:
        """Fail behaviorally when signal-free account entry is unavailable."""
        try:
            return Account(
                self.path,
                self.config,
                initialize=initialize,
                refuse_unclean=True,
            )
        except TypeError:
            self.fail("signal-free account entry is unavailable")

    def schedule(self, number: int) -> ScheduledCall:
        """Bind a unique call to the same immutable budget and policy."""
        return ScheduledCall(
            id="A_" + f"{number:032x}",
            assessment_id=self.config.id,
            comparator="new_present",
            trial_id=f"fixture-{number}",
            retry_index=1,
            round_index=1,
            policy_digest=digest(
                self.config.policies[0].model_dump(mode="json")
            ),
            config_digest=self.config.config_digest,
            authorization_digest=digest(
                self.authorization.model_dump(mode="json")
            ),
        )

    def call_record(self) -> dict[str, object]:
        """Read fixture bookkeeping without invoking account recovery."""
        with closing(sqlite3.connect(self.path / "account.sqlite3")) as conn:
            return json.loads(
                conn.execute("SELECT record FROM calls").fetchone()[0]
            )

    def test_unclean_entry_preserves_live_child_and_reservation(self) -> None:
        marker = self.root / "heartbeat"
        program = (
            "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
            "end=time.monotonic()+20; n=0\n"
            "while time.monotonic()<end:\n"
            " n+=1; p.write_text(str(n)); time.sleep(.01)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", program, str(marker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self.wait_heartbeat(marker, 0)
            with self.strict(initialize=True) as account:
                call, permit = account.claim(
                    self.schedule(1), self.authorization, 10.0
                )
                self.assertTrue(permit)
                account.ready(call.schedule.id, identify(child.pid), 11.0)
            before = self.call_record()
            previous = self.wait_heartbeat(marker, 0)
            with (
                self.assertRaisesRegex(
                    ValueError, "account_recovery_required"
                ),
                self.strict(),
            ):
                self.fail("unclean account opened")
            self.assertIsNone(child.poll())
            self.wait_heartbeat(marker, previous)
            self.assertEqual(self.call_record(), before)
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)

    def wait_heartbeat(self, path: Path, previous: int) -> int:
        """Observe actual child progress within a fixed test deadline."""
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                value = int(path.read_text())
            except (FileNotFoundError, ValueError):
                value = 0
            if value > previous:
                return value
            time.sleep(0.01)
        raise AssertionError("owned child did not make progress")

    def test_stop_survives_external_cleanup_and_default_reopen(self) -> None:
        with self.strict(initialize=True) as account:
            account.claim(self.schedule(1), self.authorization, 10.0)
        with (
            self.assertRaisesRegex(ValueError, "account_recovery_required"),
            self.strict(),
        ):
            self.fail("unclean account opened")
        record = self.call_record()
        record.update(cleaned=True, outcome="external_fixture_cleanup")
        with closing(sqlite3.connect(self.path / "account.sqlite3")) as conn:
            conn.execute("UPDATE calls SET record=?", (json.dumps(record),))
            conn.commit()
        for controller in (self.strict(), Account(self.path, self.config)):
            with (
                self.subTest(controller=controller),
                self.assertRaisesRegex(
                    ValueError, "account_recovery_required"
                ),
                controller,
            ):
                self.fail("cleanup erased the durable stop")

    def test_strict_mode_cannot_be_downgraded_before_first_refusal(
        self,
    ) -> None:
        with self.strict(initialize=True) as account:
            account.claim(self.schedule(1), self.authorization, 10.0)
        with (
            self.assertRaisesRegex(ValueError, "account_recovery_required"),
            Account(self.path, self.config),
        ):
            self.fail("default entry downgraded explicit refusal")
        self.assertFalse(self.call_record()["cleaned"])

    def test_null_money_call_limit_and_restart_remain_finite(self) -> None:
        for number in (1, 2):
            with self.strict(initialize=number == 1) as account:
                call, permit = account.claim(
                    self.schedule(number), self.authorization, 10.0 + number
                )
                self.assertTrue(permit)
                self.assertIsNone(call.cost_reservation_decimal)
                account.finish(
                    call.schedule.id, None, "synthetic_refusal", 12.0
                )
                self.assertIsNone(account.usage().cost_decimal)
        with (
            self.strict() as account,
            self.assertRaisesRegex(ValueError, "budget_call_limit"),
        ):
            account.claim(self.schedule(3), self.authorization, 13.0)

    def test_strict_controller_rejects_nested_and_parallel_ownership(
        self,
    ) -> None:
        controller = self.strict(initialize=True)
        with controller:
            with (
                self.assertRaisesRegex(ValueError, "account_busy"),
                controller,
            ):
                self.fail("same controller reentered")
            with (
                self.assertRaisesRegex(ValueError, "account_busy"),
                self.strict(),
            ):
                self.fail("second controller acquired ownership")
            self.assertEqual(controller.calls(), ())
        with self.strict() as reopened:
            self.assertEqual(reopened.calls(), ())

    def test_legacy_reused_controller_keeps_its_existing_block(self) -> None:
        with Account(self.path, self.config, initialize=True) as initial:
            initial.claim(self.schedule(1), self.authorization, 10.0)
        controller = Account(self.path, self.config)
        with (
            controller,
            self.assertRaisesRegex(ValueError, "account_recovery_required"),
        ):
            controller.claim(self.schedule(2), self.authorization, 11.0)
        record = self.call_record()
        record.update(cleaned=True)
        with closing(sqlite3.connect(self.path / "account.sqlite3")) as conn:
            conn.execute("UPDATE calls SET record=?", (json.dumps(record),))
            conn.commit()
        with (
            controller,
            self.assertRaisesRegex(ValueError, "account_recovery_required"),
        ):
            controller.claim(self.schedule(2), self.authorization, 12.0)

    def test_same_controller_refusal_survives_cleanup_and_reopen(self) -> None:
        with self.strict(initialize=True) as account:
            first, _ = account.claim(
                self.schedule(1), self.authorization, 10.0
            )
            with self.assertRaisesRegex(
                ValueError, "account_recovery_required"
            ):
                account.claim(self.schedule(2), self.authorization, 11.0)
            account.finish(
                first.schedule.id, None, "external_fixture_cleanup", 12.0
            )
            with self.assertRaisesRegex(
                ValueError, "account_recovery_required"
            ):
                account.claim(self.schedule(2), self.authorization, 13.0)
        with (
            self.assertRaisesRegex(ValueError, "account_recovery_required"),
            self.strict(),
        ):
            self.fail("same-controller refusal was forgotten")

    def test_legacy_result_names_are_not_controller_mode_or_stop(self) -> None:
        with Account(self.path, self.config, initialize=True) as account:
            account.result_binding("account-refuse-unclean", "a" * 64)
            account.result_binding("account-unclean-stop", "b" * 64)
        try:
            with Account(self.path, self.config) as account:
                self.assertEqual(
                    account.result_binding("account-refuse-unclean"), "a" * 64
                )
                self.assertEqual(
                    account.result_binding("account-unclean-stop"), "b" * 64
                )
        except ValueError as error:
            self.fail(f"ordinary legacy results changed recovery: {error}")

    def test_strict_stop_refuses_activation_of_an_existing_reservation(
        self,
    ) -> None:
        child = worker_tests.start_worker(time.monotonic() + 5)
        try:
            with self.strict(initialize=True) as account:
                first, _ = account.claim(
                    self.schedule(1), self.authorization, 10.0
                )
                account.ready(first.schedule.id, identify(child.pid), 11.0)
                with self.assertRaisesRegex(
                    ValueError, "account_recovery_required"
                ):
                    account.claim(self.schedule(2), self.authorization, 12.0)
                with self.assertRaisesRegex(
                    ValueError, "account_recovery_required"
                ):
                    account.activate(first.schedule.id, 13.0)
                self.assertFalse(self.call_record()["activation_claimed"])
                self.assertIsNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            for pipe in (child.stdin, child.stdout):
                if pipe is not None:
                    pipe.close()
