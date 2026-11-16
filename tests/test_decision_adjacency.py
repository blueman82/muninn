"""Bind an owner reply in any wording to the one display directly before."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import hook, knowledge_push, knowledge_read, query
from muninn.decision_models import DecisionIntent, VerifiedDecision
from muninn.decision_proof import verify_decision
from muninn.decision_write import submit_intent
from muninn.knowledge_models import NoteBody
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

    def test_unindexed_copy_cannot_back_a_decision(self) -> None:
        # Fork and replay copies keep no event row of their own.
        for line, code in ((0, "owner_proof"), (1, "display_proof")):
            with self.subTest(code=code):
                draft = self.stage()
                intent = self.intent(draft.id, wording="yes")
                lines = (
                    intent.owner_locator.line,
                    intent.display_locator.line,
                )
                self.conn.execute(
                    "DELETE FROM event WHERE line=?", (lines[line],)
                )
                with self.assertRaisesRegex(ValueError, code):
                    self.verify(intent)

    def test_approved_memory_gives_agents_the_approved_text(self) -> None:
        draft = self.stage(self.result([self.note(kind="decision")]))
        self.approve(self.intent(draft.id, wording="yes, save it"))
        self.saved_fixture(draft.id)
        notes = knowledge_push.knowledge_context(
            self.conn, self.scope, 12000, 30.0
        ).notes
        self.assertEqual(len(notes), 1)
        body = notes[0].body
        assert isinstance(body, NoteBody)
        self.assertEqual(body.content, "Use Python.")
        found = knowledge_read.search_knowledge(
            self.conn, "Python", self.scope
        )
        assert isinstance(found[0].body, NoteBody)
        self.assertEqual(found[0].body.content, "Use Python.")
        assert self.scope.project_id is not None
        previews = query.search(
            self.conn,
            "Python",
            cwd=self.scope.project_id,
            env={"MUNINN_HOME": str(self.home)},
        )
        self.assertIn("Use Python.", json.dumps(previews["knowledge"]))
        start = hook.session_start(
            {
                "hook_event_name": "SessionStart",
                "cwd": self.scope.project_id,
                "session_id": "s",
                "source": "startup",
            },
            "claude",
            {"MUNINN_HOME": str(self.home)},
        )
        self.assertIn(
            "1. Use Python.",
            json.dumps(start, ensure_ascii=False).replace("\\n", "\n"),
        )
