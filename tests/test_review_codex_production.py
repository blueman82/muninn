"""Check explicit ongoing authority without converting finite pilot budgets."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from muninn.review_charge import Charge, aggregate, limits
from muninn.review_models import (
    OngoingBudget,
    ReviewPolicy,
    require_pilot_budget,
)
from tests.test_learning_eval_contracts import assessment_data
from tests.test_review_shared_models import policy_data
from tools.learning_eval.models import Assessment
from tools.prepare_codex_policy import policy


def experimental_policy(home: Path) -> ReviewPolicy:
    """Construct explicitly acknowledged synthetic ongoing production terms."""
    value, _ = policy(
        home, Path("/fake-codex"), "synthetic-fixture-owner", 0.0
    )
    assert value.experimental_codex is not None
    return value.model_copy(
        update={
            "experimental_codex": value.experimental_codex.model_copy(
                update={"host_identity": "1" * 64}
            )
        }
    )


class CodexProductionPolicyTests(unittest.TestCase):
    """Require explicit ongoing authority while retaining per-call limits."""

    def setUp(self) -> None:
        self.policy = experimental_policy(Path("/synthetic/home"))

    def test_no_aggregate_ceiling_or_expiry_is_encoded(self) -> None:
        data = self.policy.model_dump(mode="json")
        self.assertIsInstance(self.policy.budget, OngoingBudget)
        for field in (
            "max_model_calls",
            "max_wall_seconds",
            "max_cost_decimal",
        ):
            self.assertNotIn(field, data["budget"])
        self.assertNotIn("expires_at", data["experimental_codex"])
        self.assertEqual(self.policy.deadline_seconds, 120.0)

    def test_ongoing_authority_refuses_caps_and_weakened_call_controls(
        self,
    ) -> None:
        for field, value in (
            ("allow_global", True),
            ("max_attempts", 2),
            ("concurrency", 2),
        ):
            data = self.policy.model_dump(mode="json")
            data[field] = value
            with self.assertRaises(ValidationError):
                ReviewPolicy.model_validate_json(json.dumps(data))
        data = self.policy.model_dump(mode="json")
        data["budget"]["max_model_calls"] = 50
        with self.assertRaises(ValidationError):
            ReviewPolicy.model_validate_json(json.dumps(data))

    def test_old_policy_serialization_and_finite_budget_are_preserved(
        self,
    ) -> None:
        old = ReviewPolicy.model_validate(policy_data())
        self.assertNotIn("experimental_codex", old.model_dump())
        require_pilot_budget(old.budget)
        with self.assertRaisesRegex(
            ValueError, "finite_pilot_budget_required"
        ):
            require_pilot_budget(self.policy.budget)

    def test_production_calls_continue_beyond_pilot_count_cost_and_time(
        self,
    ) -> None:
        calls = [Charge("unknown", 1.0, None, None, None) for _ in range(100)]
        limits(calls, self.policy.budget, None, 10_000_000.0)
        self.assertIsNone(aggregate(calls).cost_decimal)
        with self.assertRaisesRegex(ValueError, "budget_clock_reversed"):
            limits(calls, self.policy.budget, None, 0.0)

    def test_pilot_assessment_cannot_select_ongoing_authority(self) -> None:
        data = assessment_data()
        data["policies"] = [self.policy.model_dump(mode="json")]
        with self.assertRaisesRegex(
            ValidationError, "finite_pilot_budget_required"
        ):
            Assessment.model_validate_json(json.dumps(data))
