"""Preserve receipt audit reads after governed explanations are cleared."""

from __future__ import annotations

import unittest
from unittest import mock

from pydantic import ValidationError

from muninn import store
from muninn.knowledge_models import ReviewResult
from muninn.review_models import Receipt
from tests.test_review_commit import CommitCase
from tests.test_review_models import ATTEMPT, KNOWLEDGE, REQUEST


class ReceiptErasureTests(CommitCase):
    """Exercise a real no-change commit and its erased audit retry."""

    def test_cleared_reason_retries_without_reading_originals(self) -> None:
        result = self.result([])
        with store.writer_lock(self.home):
            created = self.save(result)
        self.assertEqual(created.outcome, "no_change")
        self.assertEqual(created.no_change_reason, "No change.")
        self.assertEqual(created.committed_at, 13.0)
        self.conn.execute(
            "UPDATE review_receipt SET no_change_reason=NULL "
            "WHERE request_id=?",
            (created.request_id,),
        )
        cleared = tuple(self.row("SELECT * FROM review_receipt"))
        with (
            store.writer_lock(self.home),
            mock.patch(
                "muninn.review_originals.open_regular",
                side_effect=AssertionError("original read during retry"),
            ) as original_read,
        ):
            reloaded = self.save(result)
        self.assertEqual(original_read.call_count, 0)
        self.assertEqual(reloaded.request_id, created.request_id)
        self.assertEqual(reloaded.attempt_id, created.attempt_id)
        self.assertEqual(reloaded.outcome, "no_change")
        self.assertEqual(reloaded.committed_at, 13.0)
        self.assertEqual(reloaded.applied, [])
        self.assertEqual(reloaded.unresolved_ids, [])
        self.assertIsNone(reloaded.no_change_reason)
        self.assertEqual(
            tuple(self.row("SELECT * FROM review_receipt")), cleared
        )
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 1))


class ReceiptConstraintTests(unittest.TestCase):
    """Keep producer reasons and receipt outcome constraints distinct."""

    def test_new_no_change_result_still_requires_nonempty_reason(self) -> None:
        for reason in (None, "", " "):
            with (
                self.subTest(reason=reason),
                self.assertRaises(ValidationError),
            ):
                ReviewResult(
                    schema_version=1,
                    changes=[],
                    unresolved=[],
                    no_change_reason=reason,
                )

    def test_no_change_receipt_rejects_applied_or_unresolved_items(
        self,
    ) -> None:
        for reason in (None, "No change."):
            for field, value in (
                ("applied", [{"knowledge_id": KNOWLEDGE, "revision": 1}]),
                ("unresolved_ids", ["U_" + "a" * 32]),
            ):
                with self.subTest(reason=reason, field=field):
                    data = {
                        "schema_version": 1,
                        "request_id": REQUEST,
                        "attempt_id": ATTEMPT,
                        "applied": [],
                        "unresolved_ids": [],
                        "committed_at": 13.0,
                        "outcome": "no_change",
                        "no_change_reason": reason,
                        field: value,
                    }
                    with self.assertRaisesRegex(
                        ValidationError, "invalid_no_change_receipt"
                    ):
                        Receipt.model_validate(data)

    def test_other_receipt_outcomes_reject_no_change_reason(self) -> None:
        for outcome in ("completed", "completed_unresolved"):
            with (
                self.subTest(outcome=outcome),
                self.assertRaisesRegex(
                    ValidationError, "unexpected_receipt_reason"
                ),
            ):
                Receipt.model_validate(
                    {
                        "schema_version": 1,
                        "request_id": REQUEST,
                        "attempt_id": ATTEMPT,
                        "applied": [],
                        "unresolved_ids": [],
                        "committed_at": 13.0,
                        "outcome": outcome,
                        "no_change_reason": "No change.",
                    }
                )
