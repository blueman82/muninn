"""Check explicit ongoing authority without converting finite pilot budgets."""

from __future__ import annotations

import json
import platform
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from muninn import review_codex_policy
from muninn.review_charge import Charge, aggregate, limits
from muninn.review_models import (
    OngoingBudget,
    ReviewPolicy,
    require_pilot_budget,
)
from tests.test_learning_eval_contracts import assessment_data
from tests.test_review_shared_models import policy_data
from tools.learning_eval.models import Assessment
from tools.prepare_codex_policy import default_policy, policy

DEFAULT_PROFILE_FIXTURE = (
    Path(__file__).parent / "fixtures/default_codex_policy.json"
)


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

    def test_shipped_v2_profile_uses_approved_bounds(self) -> None:
        """Use fixed shipped bounds despite profile customization."""
        selected, _ = default_policy(
            Path("/synthetic/home"),
            Path("/codex"),
            DEFAULT_PROFILE_FIXTURE.read_bytes(),
        )
        self.assertEqual(
            selected.policy_id, "muninn-codex-ongoing-production-v2"
        )
        self.assertEqual(selected.max_input_bytes, 1_048_576)
        self.assertEqual(selected.max_output_bytes, 32768)
        self.assertEqual(selected.context_bytes, 12000)
        self.assertEqual(selected.max_rounds, 2)

    def test_custom_default_profile_values_are_used(self) -> None:
        """Allow documented reductions in the repository policy profile."""
        profile = json.loads(DEFAULT_PROFILE_FIXTURE.read_text())
        profile["limits"]["context_bytes"] = 8000
        selected, _ = default_policy(
            Path("/synthetic/home"),
            Path("/codex"),
            json.dumps(profile).encode(),
        )
        self.assertEqual(selected.context_bytes, 8000)

    def test_default_profile_can_only_reduce_the_accepted_call_bounds(
        self,
    ) -> None:
        """Local profiles may reduce, but not widen, runner limits."""
        data = self.policy.model_dump(mode="json")
        data["max_input_bytes"] = 32768
        self.assertEqual(
            ReviewPolicy.model_validate_json(json.dumps(data)).max_input_bytes,
            32768,
        )
        data["max_output_bytes"] = 32769
        with self.assertRaises(ValidationError):
            ReviewPolicy.model_validate_json(json.dumps(data))

    def test_codex_version_is_not_a_policy_boundary(self) -> None:
        """Keep the explicit controls valid across Codex CLI upgrades."""
        data = self.policy.model_dump(mode="json")
        for version in ("0.163.0", "0.164.0", "any"):
            with self.subTest(version=version):
                data["runner"]["version"] = version
                selected = ReviewPolicy.model_validate_json(json.dumps(data))
                self.assertEqual(selected.runner.version, version)

    def test_revised_raw_read_limit_keeps_other_call_bounds(self) -> None:
        data = self.policy.model_dump(mode="json")
        data["policy_id"] = "muninn-codex-ongoing-production-v2"
        data["max_input_bytes"] = 1_048_576
        revised = ReviewPolicy.model_validate_json(json.dumps(data))
        self.assertEqual(revised.max_input_bytes, 1_048_576)
        data["max_input_bytes"] += 1
        with self.assertRaises(ValidationError):
            ReviewPolicy.model_validate_json(json.dumps(data))

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

    def test_recall_switches_do_not_block_learning_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            terms = self.policy.experimental_codex
            assert terms is not None
            terms = terms.model_copy(
                update={"data_home": home, "host_identity": "1" * 64}
            )
            with (
                mock.patch.object(
                    review_codex_policy,
                    "host_identity",
                    return_value=terms.host_identity,
                ),
                mock.patch.object(platform, "system", return_value="Darwin"),
                mock.patch.object(platform, "machine", return_value="arm64"),
            ):
                for files in ((), ("recall.off",), ("recall.advisory",)):
                    for name in ("recall.off", "recall.advisory"):
                        (home / name).unlink(missing_ok=True)
                    for name in files:
                        (home / name).touch()
                    with self.subTest(files=files):
                        review_codex_policy.check_activation(terms, home)
