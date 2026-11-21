"""Check recorded causes, replay and that no error text is kept."""

from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator

from muninn import review_codes, review_history, review_requests
from muninn.review_learner_result import failure_code
from muninn.review_read_budget import EVIDENCE_LIMIT_CODES
from tests.test_review_requests import RequestCase

SECRET_MARKER = "SECRET-MARKER-7f3a"
NEW_CODES = (
    "source_unavailable",
    "review_policy_required",
    "incomplete_window",
    "budget_clock_reversed",
)
_KEPT = (
    *NEW_CODES,
    "rejected_authority",
    "rejected_evidence",
    "rejected_erased",
    "rejected_fenced",
    "rejected_revision",
    "rejected_scope",
    "rejected_validation",
    "prompt_limit",
    "deadline",
    *sorted(EVIDENCE_LIMIT_CODES),
)
_DOCS = Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"


class _Model(BaseModel):
    """Raise a validation error that carries the secret in its text."""

    value: int

    @field_validator("value")
    @classmethod
    def _leak(cls, value: int) -> int:
        """Always reject the value with the secret marker in the text."""
        raise ValueError(SECRET_MARKER)


def validation_error() -> ValidationError:
    """Build a real pydantic error whose text holds the marker."""
    try:
        _Model(value=1)
    except ValidationError as exc:
        return exc
    raise AssertionError("validator did not raise")


class CodeCoverageTests(unittest.TestCase):
    """Every kept code is explained, stored and counted."""

    def test_kept_codes_survive_and_have_explanations(self) -> None:
        for code in _KEPT:
            kept = failure_code(ValueError(code), "config")
            self.assertEqual(kept, code)
            self.assertIn(kept, review_codes.EXPLANATIONS, code)
            self.assertNotEqual(
                review_codes.explain(kept), review_codes.explain("config")
            )

    def test_new_codes_keep_existing_decisions(self) -> None:
        retry: frozenset[str] = review_requests._RETRYABLE
        for code in NEW_CODES:
            self.assertIn(code, review_codes.CODES, code)
            self.assertNotIn(code, retry, code)

    def test_docs_have_a_row_for_each_new_code(self) -> None:
        text = _DOCS.read_text(encoding="utf-8")
        for code in NEW_CODES:
            self.assertIn(f"| `{code}` |", text, code)


class PrivacyTests(unittest.TestCase):
    """ADR 0004: error text never becomes a stored or shown code."""

    def test_failure_code_ignores_message_text(self) -> None:
        for error in (
            validation_error(),
            OSError(SECRET_MARKER),
            ValueError(SECRET_MARKER),
        ):
            self.assertEqual(failure_code(error, "config"), "config")

    def test_explain_never_echoes_error_text(self) -> None:
        for error in (validation_error(), OSError(SECRET_MARKER)):
            text = review_codes.explain(str(error))
            self.assertEqual(text, review_codes.explain("config"))
            self.assertNotIn(SECRET_MARKER, text)


class ReplayTests(RequestCase):
    """A failed request with a new cause replays like one with config."""

    def _covered(self, code: str) -> list[tuple[int, int]]:
        """Return the covered ranges after a failed attempt with code."""
        self.conn.execute(
            "DROP TRIGGER IF EXISTS review_request_policy_immutable"
        )
        self.conn.execute(
            "UPDATE review_request SET state='failed',policy_digest='old'"
        )
        self.conn.execute(
            "INSERT INTO review_attempt(id,request_id,generation,"
            "observed_json,started_at,progress_at,ended_at,outcome,"
            "error_code) VALUES('a1',?,1,'{}',1,1,2,'failed',?)",
            (self.request.id, code),
        )
        source = self.conn.execute("SELECT * FROM source").fetchone()
        covered = review_history._covered
        return list(covered(self.conn, source, self.policy))

    def test_old_and_new_causes_are_replayable(self) -> None:
        self.request = self.enqueue()
        for code in ("config", *NEW_CODES):
            with self.subTest(code=code):
                self.conn.execute("DELETE FROM review_attempt")
                self.assertEqual(self._covered(code), [], code)
                self.conn.execute("DELETE FROM review_attempt")

    def test_other_causes_stay_covered(self) -> None:
        self.request = self.enqueue()
        self.assertNotEqual(self._covered("rejected_scope"), [])


if __name__ == "__main__":
    unittest.main()
