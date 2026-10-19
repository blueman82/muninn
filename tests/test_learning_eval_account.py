"""Exercise persistent synthetic account claims and conservative limits."""

from __future__ import annotations

import importlib
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from muninn.review_models import ProcessIdentity
from muninn.review_process_identity import identify
from tests import test_review_worker as worker_tests
from tests.test_learning_eval_contracts import (
    FIXTURE,
    accepted_hosts,
    assessment,
    assessment_data,
    judgment,
    trial,
)
from tools.learning_eval import report as reporting
from tools.learning_eval.cases import config_digest, digest, load_cases
from tools.learning_eval.models import Assessment, TrialAuthorization
from tools.learning_eval.score import score_trials


def account_assessment(**budget: Any) -> Assessment:
    """Build an explicitly synthetic shared account for every comparator."""
    data = assessment_data()
    data["budget"].update(
        max_model_calls=12,
        max_wall_seconds=100.0,
        max_cost_decimal="12",
        conservative_call_cost_decimal="1",
        currency="USD",
    )
    data["budget"].update(budget)
    data["policies"][0]["budget"] = data["budget"].copy()
    data["comparators"] = [
        "current_muninn",
        "hermes",
        "new_present",
        "new_withheld",
    ]
    data["comparator_config_digests"] = dict.fromkeys(
        data["comparators"], "a" * 64
    )
    result = Assessment.model_validate_json(json.dumps(data))
    return result.model_copy(update={"config_digest": config_digest(result)})


class LearningAccountTests(unittest.TestCase):
    """Keep calls across retries, variants and disposable-home replacement."""

    def setUp(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("tools.learning_eval.account"),
            "persistent harness account is absent",
        )
        self.module = importlib.import_module("tools.learning_eval.account")
        self.models = importlib.import_module(
            "tools.learning_eval.trial_models"
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "account"
        self.path.mkdir(mode=0o700)
        self.config = account_assessment()
        self.home = self.root / "disposable"
        self.home.mkdir(mode=0o700)
        self.auth = TrialAuthorization(
            assessment_id=self.config.id,
            candidate_digest="a" * 64,
            config_digest="b" * 64,
            budget=self.config.budget,
            disposable_home=self.home,
            approved_by=self.config.approved_by,
            approved_at=self.config.approved_at,
        )

    def schedule(self, number: int = 1, **changes: Any) -> Any:
        """Give each model call an explicit immutable scheduled identity."""
        return self.models.ScheduledCall.model_validate(
            {
                "id": "A_" + f"{number:032x}",
                "assessment_id": self.config.id,
                "comparator": "new_present",
                "trial_id": f"synthetic-trial-{number}",
                "retry_index": 1,
                "round_index": 1,
                "policy_digest": digest(
                    self.config.policies[0].model_dump(mode="json")
                ),
                "config_digest": self.config.config_digest,
                "authorization_digest": digest(
                    self.auth.model_dump(mode="json")
                ),
            }
            | changes
        )

    def test_duplicate_claim_and_replay_binding(self) -> None:
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            call, permit = account.claim(self.schedule(), self.auth, 10.0)
            self.assertTrue(permit)
            self.assertIsNone(call.usage)
            again, permit = account.claim(self.schedule(), self.auth, 11.0)
            self.assertFalse(permit)
            self.assertEqual(call, again)
            with self.assertRaisesRegex(ValueError, "call_binding_mismatch"):
                account.claim(self.schedule(trial_id="other"), self.auth, 11.0)
            with (
                self.assertRaisesRegex(ValueError, "account_busy"),
                self.module.Account(self.path, self.config),
            ):
                self.fail("lock reentered")
        with self.module.Account(self.path, self.config) as account:
            self.assertEqual(account.calls()[0].state, "unknown")
            with self.assertRaisesRegex(
                ValueError, "account_recovery_required"
            ):
                account.claim(self.schedule(2), self.auth, 12.0)

    def test_all_comparators_reopen_and_exact_call_limit(self) -> None:
        self.config = account_assessment(max_model_calls=4)
        self.auth = self.auth.model_copy(update={"budget": self.config.budget})
        for index, label in enumerate(self.config.comparators, 1):
            with self.module.Account(
                self.path, self.config, initialize=index == 1
            ) as account:
                call, permit = account.claim(
                    self.schedule(index, comparator=label),
                    self.auth,
                    10.0 + index,
                )
                self.assertTrue(permit)
                account.finish(
                    call.schedule.id, None, "synthetic_refusal", 10.0 + index
                )
        with self.module.Account(self.path, self.config) as account:
            self.assertEqual(len(account.calls()), 4)
            with self.assertRaisesRegex(ValueError, "budget_call_limit"):
                account.claim(self.schedule(5), self.auth, 20.0)

    def test_wall_reversal_and_cost_boundaries(self) -> None:
        self.config = account_assessment(
            max_cost_decimal="1", max_wall_seconds=10.0
        )
        self.auth = self.auth.model_copy(update={"budget": self.config.budget})
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            call, _ = account.claim(self.schedule(), self.auth, 10.0)
            account.finish(call.schedule.id, None, "synthetic_refusal", 10.0)
            with self.assertRaisesRegex(ValueError, "budget_clock_reversed"):
                account.claim(self.schedule(2), self.auth, 9.0)
            with self.assertRaisesRegex(ValueError, "budget_cost_limit"):
                account.claim(self.schedule(2), self.auth, 19.0)
            with self.assertRaisesRegex(ValueError, "budget_wall_limit"):
                account.claim(self.schedule(2), self.auth, 20.0)
        with self.module.Account(self.path, self.config) as account:
            with self.assertRaisesRegex(ValueError, "budget_clock_reversed"):
                account.claim(self.schedule(2), self.auth, 19.0)
            self.assertEqual(len(account.calls()), 1)
            self.assertIsNone(account.usage().cost_decimal)

    def test_missing_corrupt_and_changed_budget_refuse_reopen(self) -> None:
        with (
            self.assertRaisesRegex(ValueError, "account_missing"),
            self.module.Account(self.path, self.config),
        ):
            pass
        with self.module.Account(self.path, self.config, initialize=True):
            pass
        with (
            self.assertRaisesRegex(ValueError, "account_binding_mismatch"),
            self.module.Account(
                self.path, account_assessment(max_model_calls=99)
            ),
        ):
            pass
        (self.path / "account.sqlite3").write_bytes(b"corrupt")
        with (
            self.assertRaisesRegex(ValueError, "account_invalid"),
            self.module.Account(self.path, self.config),
        ):
            pass

    def test_refused_schedule_remains_recorded_without_a_charge(self) -> None:
        self.config = account_assessment(max_model_calls=1)
        self.auth = self.auth.model_copy(update={"budget": self.config.budget})
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            first, _ = account.claim(self.schedule(), self.auth, 10.0)
            account.finish(first.schedule.id, None, "synthetic_refusal", 10.0)
            with self.assertRaisesRegex(ValueError, "budget_call_limit"):
                account.claim(self.schedule(2), self.auth, 11.0)
            self.assertTrue(
                hasattr(account, "scheduled"),
                "refused schedules are not retained",
            )
            self.assertEqual(len(account.scheduled()), 2)
            self.assertEqual(account.scheduled()[-1][1], "budget_call_limit")
            self.assertEqual(len(account.calls()), 1)

    def test_historical_policy_limit_survives_new_snapshot(self) -> None:
        policy = self.config.policies[0].model_copy(
            update={
                "budget": self.config.budget.model_copy(
                    update={"max_model_calls": 1}
                )
            }
        )
        self.config = self.config.model_copy(update={"policies": (policy,)})
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            call, _ = account.claim(self.schedule(), self.auth, 10.0)
            account.finish(call.schedule.id, None, "synthetic_refusal", 10.0)
        self.config = account_assessment()
        self.home = self.root / "replacement"
        self.home.mkdir(mode=0o700)
        self.auth = self.auth.model_copy(update={"disposable_home": self.home})
        with (
            self.module.Account(self.path, self.config) as account,
            self.assertRaisesRegex(ValueError, "budget_call_limit"),
        ):
            account.claim(self.schedule(2), self.auth, 11.0)

    def test_missing_charge_and_currency_mismatch_refuse(self) -> None:
        self.config = account_assessment(conservative_call_cost_decimal=None)
        self.auth = self.auth.model_copy(update={"budget": self.config.budget})
        with (
            self.module.Account(
                self.path, self.config, initialize=True
            ) as account,
            self.assertRaisesRegex(ValueError, "budget_charge_unavailable"),
        ):
            account.claim(self.schedule(), self.auth, 10.0)

    def test_account_report_retains_refused_denominator_and_uncertainty(
        self,
    ) -> None:
        self.config = account_assessment(max_model_calls=1)
        self.auth = self.auth.model_copy(update={"budget": self.config.budget})
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            call, _ = account.claim(self.schedule(), self.auth, 10.0)
            account.finish(call.schedule.id, None, "synthetic_refusal", 10.0)
            with self.assertRaises(ValueError):
                account.claim(self.schedule(2), self.auth, 11.0)
            self.assertTrue(
                callable(getattr(reporting, "with_account", None)),
                "account uncertainty is absent from reports",
            )
            report = reporting.with_account(
                score_trials((), (), self.config), account
            )
            self.assertEqual(
                report.trial_ids, ("synthetic-trial-1", "synthetic-trial-2")
            )
            self.assertIn(
                "budget_refusal_or_uncertain_spend", report.limitations
            )
            self.assertEqual(report.status, "unverified")

    def test_unresolved_claim_blocks_new_scheduling_in_same_controller(
        self,
    ) -> None:
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            account.claim(self.schedule(), self.auth, 10.0)
            with self.assertRaisesRegex(
                ValueError, "account_recovery_required"
            ):
                account.claim(self.schedule(2), self.auth, 11.0)

    def test_retries_have_fresh_separately_charged_identity(self) -> None:
        policy = self.config.policies[0].model_copy(
            update={"max_attempts": 2, "retry_backoff_seconds": [0.0]}
        )
        self.config = self.config.model_copy(update={"policies": (policy,)})
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            first, _ = account.claim(self.schedule(), self.auth, 10.0)
            account.finish(first.schedule.id, None, "synthetic_refusal", 10.0)
            second, permit = account.claim(
                self.schedule(
                    2, trial_id=first.schedule.trial_id, retry_index=2
                ),
                self.auth,
                11.0,
            )
            self.assertTrue(permit)
            self.assertEqual(second.reserved_at, 11.0)
            self.assertEqual(len(account.calls()), 2)
            self.assertEqual(
                sum(
                    float(c.cost_reservation_decimal) for c in account.calls()
                ),
                2.0,
            )

    def test_stronger_current_policy_charge_is_reserved(self) -> None:
        policy = self.config.policies[0].model_copy(
            update={
                "budget": self.config.budget.model_copy(
                    update={"conservative_call_cost_decimal": "2"}
                )
            }
        )
        self.config = self.config.model_copy(update={"policies": (policy,)})
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            call, _ = account.claim(self.schedule(), self.auth, 10.0)
            self.assertEqual(call.cost_reservation_decimal, "2")

    def test_budget_refusal_overrides_successful_measurement(self) -> None:
        case = load_cases(FIXTURE)[0]
        config = assessment(
            criteria=[
                {
                    "metric": "unsupported_saves",
                    "unit": "proportion",
                    "direction": "at_most",
                    "threshold": 1.0,
                    "interval_method": "none",
                    "confidence": None,
                }
            ]
        )
        item = trial(
            case.id, judgment=judgment(0, 1), outcome="budget_call_limit"
        )
        report = score_trials(
            (case,), (item,), config, host_results=accepted_hosts(config)
        )
        self.assertEqual(report.status, "inconclusive")
        self.assertIn("budget_refusal_or_uncertain_spend", report.limitations)
        blocked = item.model_copy(update={"outcome": "erasure_resurrection"})
        self.assertEqual(
            score_trials((case,), (blocked,), config).status, "revise"
        )

    def test_recovery_signals_only_recorded_verified_fake_child(self) -> None:
        child = worker_tests.start_worker(time.monotonic() + 5.0)
        try:
            with self.module.Account(
                self.path, self.config, initialize=True
            ) as account:
                call, _ = account.claim(self.schedule(), self.auth, 10.0)
                account.ready(call.schedule.id, identify(child.pid), 11.0)
            with self.module.Account(self.path, self.config) as account:
                self.assertEqual(child.wait(timeout=2), -9)
                self.assertTrue(account.calls()[0].cleaned)
                self.assertEqual(account.calls()[0].state, "unknown")
                self.assertEqual(
                    account.calls()[0].cost_reservation_decimal, "1"
                )
                self.assertTrue(
                    account.claim(self.schedule(2), self.auth, 12.0)[1]
                )
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for pipe in (child.stdin, child.stdout):
                if pipe is not None:
                    pipe.close()

    def test_conflicting_recovery_identity_refuses_scheduling(self) -> None:
        process = ProcessIdentity(
            pid=999999, created="synthetic", pgid=999999, job_marker=None
        )
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            call, _ = account.claim(self.schedule(), self.auth, 10.0)
            with mock.patch.object(
                self.module, "identify", return_value=process
            ):
                account.ready(call.schedule.id, process, 11.0)
        with (
            mock.patch.object(
                self.module,
                "recover_group",
                side_effect=ValueError("process_identity_unverified"),
            ) as recover,
            self.module.Account(self.path, self.config) as account,
            self.assertRaisesRegex(ValueError, "account_recovery_required"),
        ):
            recover.assert_called_once_with(process)
            account.claim(self.schedule(2), self.auth, 12.0)
