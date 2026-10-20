"""Check explicit monetary bounds and their frozen policy identity."""

from __future__ import annotations

import hashlib
import json
import unittest
from decimal import Decimal

from pydantic import ValidationError

from muninn.review_json import canonical_json
from muninn.review_models import PilotBudget, ReviewPolicy
from tests.test_learning_eval_contracts import (
    FIXTURE,
    assessment_data,
    trial,
)
from tests.test_review_shared_models import policy_data
from tools.learning_eval.cases import digest, load_cases
from tools.learning_eval.models import Assessment
from tools.learning_eval.score import score_trials


class ReviewCostContractTests(unittest.TestCase):
    """Keep approved cost bounds explicit, strict and policy-bound."""

    def test_bound_is_required_but_explicit_null_is_preserved(self) -> None:
        data = policy_data()["budget"]
        omitted = dict(data)
        del omitted["conservative_call_cost_decimal"]
        with self.assertRaises(ValidationError) as raised:
            PilotBudget.model_validate(omitted)
        self.assertEqual(
            [(e["loc"], e["type"]) for e in raised.exception.errors()],
            [(("conservative_call_cost_decimal",), "missing")],
        )
        budget = PilotBudget.model_validate(data)
        self.assertIsNone(budget.conservative_call_cost_decimal)
        self.assertIn("conservative_call_cost_decimal", budget.model_dump())
        self.assertIsNone(
            json.loads(canonical_json(budget))[
                "conservative_call_cost_decimal"
            ]
        )

    def test_bound_preserves_valid_finite_nonnegative_decimal_strings(
        self,
    ) -> None:
        for value in ("0", "0.00", "2.50", "1e-3", "-0", "1e1000"):
            with self.subTest(value=value):
                budget = PilotBudget.model_validate(
                    {
                        **policy_data()["budget"],
                        "conservative_call_cost_decimal": value,
                        "currency": "USD",
                    }
                )
                self.assertEqual(budget.conservative_call_cost_decimal, value)
                self.assertEqual(
                    json.loads(canonical_json(budget))[
                        "conservative_call_cost_decimal"
                    ],
                    value,
                )

    def test_approval_metadata_does_not_admit_invalid_bound_values(
        self,
    ) -> None:
        for value in (
            "-1",
            "NaN",
            "sNaN",
            "Infinity",
            "-Infinity",
            "not-money",
            "",
            " ",
            0,
            1.5,
            True,
            Decimal("2.50"),
            [],
            {},
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PilotBudget.model_validate(
                    {
                        **policy_data()["budget"],
                        "conservative_call_cost_decimal": value,
                        "currency": "USD",
                    }
                )

    def test_json_bound_rejects_numeric_and_boolean_coercion(self) -> None:
        for value in (0, 1.5, True, "-1", "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PilotBudget.model_validate_json(
                    json.dumps(
                        {
                            **policy_data()["budget"],
                            "conservative_call_cost_decimal": value,
                            "currency": "USD",
                        }
                    )
                )

    def test_currency_is_required_for_either_amount_including_zero(
        self,
    ) -> None:
        for cap, bound in (("0", None), (None, "0"), ("2", "0.50")):
            data = {
                **policy_data()["budget"],
                "max_cost_decimal": cap,
                "conservative_call_cost_decimal": bound,
            }
            for currency in (None, "", " "):
                with (
                    self.subTest(cap=cap, bound=bound, currency=currency),
                    self.assertRaises(ValidationError),
                ):
                    PilotBudget.model_validate({**data, "currency": currency})
            self.assertEqual(
                PilotBudget.model_validate(
                    {**data, "currency": "USD"}
                ).currency,
                "USD",
            )

    def test_budget_remains_frozen_and_extra_fields_are_forbidden(
        self,
    ) -> None:
        data = policy_data()["budget"]
        budget = PilotBudget.model_validate(data)
        with self.assertRaises(ValidationError):
            budget.conservative_call_cost_decimal = "0.50"
        with self.assertRaises(ValidationError):
            PilotBudget.model_validate({**data, "guessed_cost": "0.50"})

    def test_whole_policy_hash_binds_the_explicit_bound(self) -> None:
        data = policy_data()
        data["budget"].update(
            conservative_call_cost_decimal="0.50", currency="USD"
        )
        original = ReviewPolicy.model_validate(data)
        expected = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        self.assertEqual(canonical_json(original), expected)
        original_digest = hashlib.sha256(expected.encode()).hexdigest()
        self.assertEqual(
            digest(original.model_dump(mode="json")), original_digest
        )
        data["budget"]["conservative_call_cost_decimal"] = "0.75"
        changed = ReviewPolicy.model_validate(data)
        self.assertNotEqual(
            digest(changed.model_dump(mode="json")), original_digest
        )
        self.assertEqual(
            original.budget.conservative_call_cost_decimal, "0.50"
        )
        self.assertEqual(
            changed.budget.approved_by, original.budget.approved_by
        )
        self.assertEqual(
            changed.budget.approved_at, original.budget.approved_at
        )

    def test_frozen_trial_rejects_a_changed_bound_with_same_approval(
        self,
    ) -> None:
        case = load_cases(FIXTURE)[0]
        data = assessment_data()
        data["policies"][0]["budget"].update(
            conservative_call_cost_decimal="0.50", currency="USD"
        )
        original = ReviewPolicy.model_validate(data["policies"][0])
        attempt = trial(
            case.id, policy_digest=digest(original.model_dump(mode="json"))
        )
        data["policies"][0]["budget"][
            "conservative_call_cost_decimal"
        ] = "0.75"
        changed = Assessment.model_validate_json(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "unfrozen_policy"):
            score_trials((case,), (attempt,), changed)
