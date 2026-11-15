"""Bind an owner reply in any wording to the one display directly before."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from muninn.decision_models import DecisionIntent, VerifiedDecision
from muninn.decision_proof import verify_decision
from muninn.decision_write import submit_intent
from muninn.review_read_budget import ReadBudget
from tests.classify_support import item
from tests.decision_support import DecisionCase
from tests.ingest_support import fc_output, primary
from tests.test_classify import function_call, reply, user_msg


def event(ordinal: int) -> dict[str, Any]:
    """Build a provider event line that projects no permitted field."""
    return item({"type": "token_count", "info": None}, ordinal, "event_msg")


class DecisionAdjacencyTests(DecisionCase):
    """Accept a plain reply only straight after its single display."""

    def setUp(self) -> None:
        project = tempfile.TemporaryDirectory()
        self.addCleanup(project.cleanup)
        with mock.patch(
            "tests.test_review_requests.primary",
            return_value=primary(cwd=str(Path(project.name).resolve())),
        ):
            super().setUp()

    def verify(self, intent: DecisionIntent) -> VerifiedDecision:
        """Verify with one operation allowance inside caller ownership."""
        return self.transaction(
            lambda: verify_decision(
                self.conn,
                self.home,
                intent,
                self.policy,
                ReadBudget(self.policy.max_input_bytes),
            )
        )

    def test_yes_after_display_saves(self) -> None:
        draft = self.stage()
        self.between = [event(6), event(7)]
        intent = self.intent(draft.id, wording="yes, save it")
        self.approve(intent)
        receipt = self.saved_fixture(draft.id)
        self.assertEqual(receipt.outcome, "completed")

    def test_message_or_tool_call_between_refuses(self) -> None:
        for between in (
            [reply(6, "Other text.")],
            [user_msg(6, "wait")],
            [
                function_call(6, "exec_command", '{"cmd":"x"}', "a"),
                fc_output(7, "a", "done"),
            ],
        ):
            with self.subTest(between=between):
                draft = self.stage()
                self.between = between
                intent = self.intent(draft.id, wording="yes")
                with self.assertRaisesRegex(ValueError, "display_proof"):
                    self.verify(intent)

    def test_foreign_proposal_id_in_display_refuses(self) -> None:
        other = "D_" + "1" * 32
        draft = self.stage(
            self.result([self.note(content=f"Use Python, not {other}.")])
        )
        intent = self.intent(draft.id, wording="yes")
        with self.assertRaisesRegex(ValueError, "display_proof"):
            self.verify(intent)

    def test_one_reply_backs_one_decision(self) -> None:
        first = self.stage()
        second = self.stage(self.result([self.note(content="Other fact.")]))
        intent = self.intent(first.id, wording="yes")
        self.transaction(lambda: submit_intent(self.conn, intent, 16.0))
        self.transaction(lambda: submit_intent(self.conn, intent, 16.5))
        reused = intent.model_copy(update={"candidate_id": second.id})
        with self.assertRaisesRegex(ValueError, "owner_proof"):
            self.transaction(lambda: submit_intent(self.conn, reused, 17.0))
