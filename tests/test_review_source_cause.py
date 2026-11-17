"""Check the named source cause and that no error text is kept."""

from __future__ import annotations

import unittest

from pydantic import BaseModel, ValidationError, field_validator

from muninn import obs_log, review_recovery, review_requests
from muninn.review_recovery_codes import CONTAINED, EXPLANATIONS, SHARED
from muninn.review_supervisor_result import failure_code

SECRET_MARKER = "SECRET-MARKER-7f3a"


class _Model(BaseModel):
    """Raise a validation error that carries the secret in its text."""

    value: int

    @field_validator("value")
    @classmethod
    def _leak(cls, value: int) -> int:
        """Always reject the value, with the marker in the message."""
        raise ValueError(SECRET_MARKER)


def _validation_error() -> ValidationError:
    """Build a real pydantic error whose text holds the marker."""
    try:
        _Model(value=1)
    except ValidationError as exc:
        return exc
    raise AssertionError("validator did not raise")


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


class PrivacyTests(unittest.TestCase):
    """ADR 0004: error text never becomes a stored or shown code."""

    def test_failure_code_ignores_message_text(self) -> None:
        for error in (
            _validation_error(),
            OSError(SECRET_MARKER),
            ValueError(SECRET_MARKER),
        ):
            code = failure_code(error, "config")
            self.assertEqual(code, "config")
            self.assertNotIn(SECRET_MARKER, code)

    def test_explain_never_echoes_error_text(self) -> None:
        for error in (_validation_error(), OSError(SECRET_MARKER)):
            detail = review_recovery.explain(str(error), "launch")
            self.assertEqual(detail["code"], "config")
            self.assertNotIn(SECRET_MARKER, "".join(detail.values()))


if __name__ == "__main__":
    unittest.main()
