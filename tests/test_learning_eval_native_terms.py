"""Keep original canary charges, time and identity across account restarts."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.test_learning_eval_account import account_assessment
from tools.learning_eval.account import Account
from tools.learning_eval.cases import config_digest, digest
from tools.learning_eval.models import TrialAuthorization
from tools.learning_eval.trial_models import ScheduledCall


def terms_data(root: Path, assessment: Any, started: float) -> dict[str, Any]:
    """Create a private artificial carry-in without invoking a provider."""
    started = round(started, 6)
    module = importlib.import_module("tools.learning_eval.native_terms")
    ledger = {
        "start_utc": datetime.fromtimestamp(started, UTC).isoformat(),
        "approved_limits": {
            "max_cli_launches": 50,
            "max_wall_seconds": 14400,
            "max_additional_spend_usd": "250",
            "unknown_cost_reservation_usd_per_launch": "5",
        },
        "scope": {
            "host": "Darwin arm64",
            "provider": "codex_cli",
            "version": "0.163.0",
        },
        "launches": [
            {
                "ordinal": 1,
                "reserved_usd": "5",
                "actual_cost_usd": None,
                "actual_model_requests": None,
                "state": "completed",
                "exit_code": 0,
                "reserved_at_utc": datetime.fromtimestamp(
                    started, UTC
                ).isoformat(),
            }
        ],
    }
    for name, value in (
        ("canary.json", ledger),
        ("owner.json", {"approval": "fixture"}),
    ):
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    return {
        "assessment_id": assessment.id,
        "account_id": assessment.budget.account_id,
        "account_path": str(root / "account"),
        "receipt_path": str(root / "import.json"),
        "owner_boundary_path": str(root / "owner.json"),
        "owner_boundary_digest": hashlib.sha256(
            (root / "owner.json").read_bytes()
        ).hexdigest(),
        "carry_in_path": str(root / "canary.json"),
        "carry_in_digest": hashlib.sha256(
            (root / "canary.json").read_bytes()
        ).hexdigest(),
        "started_at": started,
        "guarantee_set": "cli-launch-reservation-only-v1",
        "carried_launches": 1,
        "carried_charge": "5",
        "host_identity": module.host_identity(),
    }


@unittest.skipIf(
    sys.platform == "win32", "requires POSIX evaluation account storage"
)
class NativeTermsTests(unittest.TestCase):
    """Exercise carry-in via public Account operations, never seeded rows."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "account"
        self.path.mkdir(mode=0o700)
        self.home = self.root / "disposable"
        self.home.mkdir(mode=0o700)
        self.assertIsNotNone(
            importlib.util.find_spec("tools.learning_eval.native_terms")
        )
        self.native = importlib.import_module(
            "tools.learning_eval.native_terms"
        )
        self.config = account_assessment(
            max_model_calls=49,
            max_wall_seconds=14400.0,
            max_cost_decimal="245",
            conservative_call_cost_decimal="5",
        )
        policy = self.config.policies[0].model_copy(update={"max_attempts": 1})
        self.config = self.config.model_copy(update={"policies": (policy,)})
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        self.auth = TrialAuthorization(
            assessment_id=self.config.id,
            candidate_digest="a" * 64,
            config_digest="b" * 64,
            budget=self.config.budget,
            disposable_home=self.home,
            approved_by=self.config.approved_by,
            approved_at=self.config.approved_at,
        )
        self.terms = self.native.NativeTerms.model_validate_json(
            json.dumps(terms_data(self.root, self.config, 100.0))
        )

    def schedule(self, number: int) -> ScheduledCall:
        """Name an independent launch slot in the shared account."""
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
            authorization_digest=digest(self.auth.model_dump(mode="json")),
        )

    def test_original_charge_and_49_remaining_claims(self) -> None:
        with Account(self.path, self.config, initialize=True) as account:
            self.native.bind_account(account, self.terms, 101.0)
            for number in range(1, 50):
                account.claim(self.schedule(number), self.auth, 101.0)
                account.finish(
                    self.schedule(number).id, None, "fixture", 101.0
                )
            self.assertEqual(
                self.native.account_totals(account, self.terms), (50, "250")
            )
            with self.assertRaisesRegex(ValueError, "budget_call_limit"):
                account.claim(self.schedule(50), self.auth, 101.0)
        with Account(self.path, self.config) as account:
            self.native.bind_account(account, self.terms, 102.0)
            self.assertEqual(len(account.calls()), 49)
            with self.assertRaisesRegex(ValueError, "budget_wall_limit"):
                self.native.bind_account(account, self.terms, 14500.0)

    def test_reset_changed_carry_and_wrong_identity_refuse(self) -> None:
        with Account(self.path, self.config, initialize=True) as account:
            self.native.bind_account(account, self.terms, 101.0)
            with self.assertRaises(ValueError):
                self.native.bind_account(
                    account,
                    self.terms.model_copy(update={"host_identity": "a" * 64}),
                    101.0,
                )
        (self.path / "account.sqlite3").unlink()
        with (
            Account(self.path, self.config, initialize=True) as account,
            self.assertRaisesRegex(ValueError, "native_account_reset"),
        ):
            self.native.bind_account(account, self.terms, 101.0)
        self.terms.carry_in_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "native_authority_changed"):
            self.native.validate_terms(self.terms, self.config, 101.0)

    def test_policy_budget_extra_canary_and_unresolved_claim_refuse(
        self,
    ) -> None:
        policy = self.config.policies[0]
        for changes in (
            {"max_model_calls": 50},
            {"max_cost_decimal": "250"},
            {"conservative_call_cost_decimal": "1"},
            {"max_wall_seconds": 14401.0},
        ):
            changed = policy.model_copy(
                update={"budget": policy.budget.model_copy(update=changes)}
            )
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "native_budget_binding"),
            ):
                self.native.validate_terms(
                    self.terms,
                    self.config.model_copy(update={"policies": (changed,)}),
                    101.0,
                )
        ledger = json.loads(self.terms.carry_in_path.read_bytes())
        original = self.terms.carry_in_path.read_bytes()
        ledger["launches"].append(ledger["launches"][0])
        self.terms.carry_in_path.write_text(json.dumps(ledger))
        changed_terms = self.terms.model_copy(
            update={
                "carry_in_digest": hashlib.sha256(
                    self.terms.carry_in_path.read_bytes()
                ).hexdigest()
            }
        )
        with self.assertRaisesRegex(ValueError, "native_carry_in_binding"):
            self.native.validate_terms(changed_terms, self.config, 101.0)
        self.terms.carry_in_path.write_bytes(original)
        with Account(self.path, self.config, initialize=True) as account:
            self.native.bind_account(account, self.terms, 101.0)
            account.claim(self.schedule(1), self.auth, 101.0)
        with Account(self.path, self.config) as account:
            self.native.bind_account(account, self.terms, 102.0)
            self.assertEqual(
                self.native.account_totals(account, self.terms), (2, "10")
            )
            with self.assertRaisesRegex(
                ValueError, "account_recovery_required"
            ):
                account.claim(self.schedule(2), self.auth, 102.0)

    def test_tighter_wall_budget_keeps_original_start(self) -> None:
        budget = self.config.budget.model_copy(
            update={"max_wall_seconds": 100.0}
        )
        changed = self.config.model_copy(update={"budget": budget})
        with self.assertRaisesRegex(ValueError, "budget_wall_limit"):
            self.native.validate_terms(self.terms, changed, 200.0)

    def test_two_launch_carry_requires_verified_predecessor(self) -> None:
        terms = self.terms.model_copy(
            update={"carried_launches": 2, "carried_charge": "10"}
        )
        with self.assertRaisesRegex(ValueError, "native_predecessor"):
            self.native.validate_terms(terms, self.config, 101.0)
