"""Prove original rendered owner choices through actual checked application."""

from __future__ import annotations

import json
from typing import Any

from muninn import review_requests
from muninn.decision_models import CandidateInput, QuestionDisposition
from tests.test_cli_memory_decisions import DecisionCliCase


class MemoryDecisionE2ETests(DecisionCliCase):
    """Connect actual CLI text, physical originals and atomic saved receipt."""

    def submit_cli(self, ident: str, **options: Any) -> dict[str, object]:
        """Submit coordinates for real indexed original fields."""
        intent = self.intent(ident, **options)
        path = self.tmp / "intent.json"
        path.write_text(intent.model_dump_json())
        code, view = self.call(
            "know", "decision", "submit", "--input", str(path)
        )
        self.assertEqual(code, 0, view)
        return view

    def test_question_actual_rendered_choice_application_and_replay(
        self,
    ) -> None:
        question = {
            "claim": "Choose",
            "question": "Which rule applies?",
            "affected": [],
            "evidence_refs": [],
        }
        uid = self.save(self.result([], [question])).unresolved_ids[0]
        self.context = self.start(2, 2)
        path = self.tmp / "question-candidate.json"
        path.write_text(
            CandidateInput(
                request_id=self.context.request_id,
                result=self.result([self.note()]),
                disposition=QuestionDisposition(
                    unresolved_id=uid,
                    answer="Use Python.",
                    affected=(),
                    complete=True,
                ),
            ).model_dump_json()
        )
        before = self.projection()
        code, listed = self.call("know", "unresolved", "--limit", "1")
        self.assertEqual((code, listed["unresolved"][0]["id"]), (0, uid))
        code, unresolved = self.call("know", "unresolved", uid)
        self.assertEqual(self.projection(), before)
        self.assertEqual((code, unresolved["state"]), (0, "open"))
        code, draft = self.call(
            "know", "decision", "stage", "--input", str(path)
        )
        self.assertEqual(code, 0, draft)
        ident = draft["id"]
        code, display = self.call("know", "decision", "show", ident)
        self.assertEqual(code, 0, display)
        text = display["text"]
        phrase = json.loads(text)["owner_decision"]["approve"]
        view = self.submit_cli(ident, display=text, wording=phrase)
        self.assertEqual(view["state"], "approved_pending")
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 20.0
            )
        )
        code, saved = self.call("know", "decision", "apply", ident)
        self.assertEqual((code, saved["state"]), (0, "saved"), saved)
        self.assertEqual(len(saved["receipt"]["applied"]), 1)
        self.assertEqual(
            self.call("know", "unresolved", uid)[1]["state"], "resolved"
        )
        before = self.projection()
        self.assertEqual(
            self.call("know", "decision", "apply", ident)[1]["receipt"],
            saved["receipt"],
        )
        self.assertEqual(self.projection(), before)

    def test_rendered_rejection_and_edit_require_new_approval(self) -> None:
        old = str(self.stage_cli()["id"])
        new = str(self.stage_cli()["id"])
        display = self.call("know", "decision", "show", new)[1]["text"]
        edited = self.submit_cli(
            old, action="edit", replacement_id=new, display=display
        )
        self.assertEqual(edited["state"], "superseded")
        self.assertEqual(
            self.call("know", "decision", "status", new)[1]["state"],
            "waiting_verification",
        )
        rejected_id = str(self.stage_cli()["id"])
        rejected_display = self.call("know", "decision", "show", rejected_id)[
            1
        ]["text"]
        phrase = json.loads(rejected_display)["owner_decision"]["reject"]
        rejected = self.submit_cli(
            rejected_id,
            action="reject",
            display=rejected_display,
            wording=phrase,
        )
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)
        phrase = json.loads(display)["owner_decision"]["approve"]
        accepted = self.submit_cli(new, display=display, wording=phrase)
        self.assertEqual(accepted["state"], "approved_pending")
        independent = str(self.stage_cli()["id"])
        independent_display = self.call(
            "know", "decision", "show", independent
        )[1]["text"]
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 20.0
            )
        )
        code, saved = self.call("know", "decision", "apply", new)
        self.assertEqual((code, saved["state"]), (0, "saved"), saved)
        late = self.submit_cli(
            new,
            action="edit",
            replacement_id=independent,
            display=independent_display,
        )
        self.assertEqual(late["state"], "saved")
        self.assertEqual(late["receipt"], saved["receipt"])
        self.assertEqual(
            self.call("know", "decision", "status", independent)[1]["state"],
            "waiting_verification",
        )

    def test_indexing_wait_then_approve_while_paused_and_refused_apply(
        self,
    ) -> None:
        ident = str(self.stage_cli()["id"])
        text = self.call("know", "decision", "show", ident)[1]["text"]
        intent = self.intent(ident, display=text)
        cursor = self.row(
            "SELECT cursor_line FROM source WHERE thread_id=?",
            (intent.owner_locator.source.thread_id,),
        )[0]
        self.conn.execute(
            "UPDATE source SET cursor_line=? WHERE thread_id=?",
            (
                intent.display_locator.line - 1,
                intent.owner_locator.source.thread_id,
            ),
        )
        path = self.tmp / "pending.json"
        path.write_text(intent.model_dump_json())
        code, pending = self.call(
            "know", "decision", "submit", "--input", str(path)
        )
        self.assertEqual(
            (code, pending["state"], pending["code"]),
            (0, "waiting_verification", "waiting_index"),
        )
        self.conn.execute(
            "UPDATE source SET cursor_line=? WHERE thread_id=?",
            (cursor, intent.owner_locator.source.thread_id),
        )
        self.conn.execute("UPDATE review_control SET admission=0")
        code, accepted = self.call(
            "know", "decision", "submit", "--input", str(path)
        )
        self.assertEqual(
            (code, accepted["state"]), (0, "approved_pending"), accepted
        )
        code, blocked = self.call("know", "decision", "apply", ident)
        self.assertEqual(
            (code, blocked["code"]), (0, "learning_paused"), blocked
        )
        self.assertIsNone(blocked["receipt"])
