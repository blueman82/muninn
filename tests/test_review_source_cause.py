"""Check the named source cause and that no error text is kept."""

from __future__ import annotations

import unittest

from muninn import obs_log, review_recovery, review_requests
from muninn.review_recovery_codes import CONTAINED, EXPLANATIONS, SHARED
from muninn.review_supervisor_result import failure_code
from tests.secret_support import SECRET_MARKER, validation_error


class SourceCauseTests(unittest.TestCase):
    """source_unavailable has its own code and explanation."""

    def test_failure_code_keeps_source_unavailable(self) -> None:
        self.assertEqual(
            failure_code(ValueError("source_unavailable"), "config"),
            "source_unavailable",
        )

    def test_explanation_is_specific(self) -> None:
        detail = review_recovery.explain("source_unavailable", "launch")
        self.assertEqual(detail["code"], "source_unavailable")
        self.assertNotEqual(detail["cause"], EXPLANATIONS["config"][0])
        self.assertIn("source", detail["cause"].lower())

    def test_code_is_stored_and_counted(self) -> None:
        errors: frozenset[str] = vars(review_requests)["_ERRORS"]
        self.assertIn("source_unavailable", errors)
        logged: frozenset[str] = vars(obs_log)["_REVIEW_ERRORS"]
        self.assertIn("source_unavailable", logged)

    def test_decision_sets_are_unchanged(self) -> None:
        retry: frozenset[str] = vars(review_requests)["_RETRYABLE"]
        self.assertEqual(
            retry, frozenset({"deadline", "unavailable", "rejected_revision"})
        )
        self.assertNotIn("source_unavailable", CONTAINED)
        self.assertNotIn("source_unavailable", SHARED)
        self.assertNotIn("config", CONTAINED)
        self.assertIn("config", SHARED)


NEW_CODES = (
    "source_unavailable",
    "review_policy_required",
    "incomplete_window",
    "budget_clock_reversed",
)


class KeptCodeTests(unittest.TestCase):
    """Each kept code has an explanation and no new decision."""

    def test_failure_code_keeps_each_code_with_an_explanation(self) -> None:
        for code in NEW_CODES:
            self.assertEqual(failure_code(ValueError(code), "config"), code)
            self.assertNotEqual(
                review_recovery.explain(code, "launch")["code"], "config"
            )
        self.assertIn("rejected_fenced", EXPLANATIONS)

    def test_codes_are_stored_but_not_retried_or_contained(self) -> None:
        errors: frozenset[str] = vars(review_requests)["_ERRORS"]
        logged: frozenset[str] = vars(obs_log)["_REVIEW_ERRORS"]
        for code in NEW_CODES:
            self.assertIn(code, errors)
            self.assertIn(code, logged)
            self.assertNotIn(code, CONTAINED)
            self.assertNotIn(code, SHARED)
        retry: frozenset[str] = vars(review_requests)["_RETRYABLE"]
        self.assertFalse(retry.intersection(NEW_CODES))


class PrivacyTests(unittest.TestCase):
    """ADR 0004: error text never becomes a stored or shown code."""

    def test_failure_code_ignores_message_text(self) -> None:
        for error in (
            validation_error(),
            OSError(SECRET_MARKER),
            ValueError(SECRET_MARKER),
        ):
            code = failure_code(error, "config")
            self.assertEqual(code, "config")
            self.assertNotIn(SECRET_MARKER, code)

    def test_explain_never_echoes_error_text(self) -> None:
        for error in (validation_error(), OSError(SECRET_MARKER)):
            detail = review_recovery.explain(str(error), "launch")
            self.assertEqual(detail["code"], "config")
            self.assertNotIn(SECRET_MARKER, "".join(detail.values()))


if __name__ == "__main__":
    unittest.main()
