"""Check candidate staging against real originals and later batch defects."""

from __future__ import annotations

import json
from unittest import mock

from muninn import decision_candidates, knowledge_write, store
from muninn.decision_models import CandidateInput, QuestionDisposition
from tests.decision_support import DecisionCase


class StagingSafetyTests(DecisionCase):
    """Keep missing owner support separate from other validation checks."""

    def test_missing_owner_cannot_hide_later_target_batch_or_unknown_fault(
        self,
    ) -> None:
        saved = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(3, 3)
        missing_owner = self.note(
            kind="preference", evidence_refs=[self.evidence[1].id]
        )
        replacement = self.note(
            action="replace",
            knowledge_id=saved.knowledge_id,
            expected_revision=1,
            evidence_refs=[self.evidence[1].id],
        )
        for fault, changes, unresolved, code in (
            (
                "unknown",
                [missing_owner, self.note(evidence_refs=["E_" + "f" * 32])],
                [],
                "rejected_evidence",
            ),
            (
                "stale",
                [missing_owner, {**replacement, "expected_revision": 2}],
                [],
                "rejected_revision",
            ),
            (
                "duplicate",
                [missing_owner, replacement, replacement],
                [],
                "rejected_revision",
            ),
            (
                "overlap",
                [missing_owner, replacement],
                [
                    {
                        "claim": "Question",
                        "question": "Which applies?",
                        "affected": [saved.model_dump()],
                        "evidence_refs": [],
                    }
                ],
                "rejected_revision",
            ),
        ):
            with self.subTest(fault=fault):
                before = self.projection()
                with self.assertRaisesRegex(ValueError, "^" + code + "$"):
                    self.stage(self.result(changes, unresolved))
                self.assertEqual(self.projection(), before)

    def test_erased_support_and_non_owner_source_fault_refuse(self) -> None:
        before = self.projection()
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(
            "UPDATE review_evidence SET state='erased',excerpt=NULL"
        )
        value = CandidateInput(
            request_id=self.context.request_id,
            result=self.result([self.note()]),
            disposition=None,
        )
        with self.assertRaisesRegex(ValueError, "^rejected_evidence$"):
            decision_candidates.stage_candidate(
                self.conn, self.home, value, 14.0
            )
        self.conn.rollback()
        self.assertEqual(self.projection(), before)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE source SET thread_class='subagent'")
        with self.assertRaisesRegex(ValueError, "^rejected_evidence$"):
            decision_candidates.stage_candidate(
                self.conn, self.home, value, 14.0
            )
        self.conn.rollback()
        self.assertEqual(self.projection(), before)

    def test_original_reads_use_one_budget_and_no_new_connection_or_root(
        self,
    ) -> None:
        with (
            mock.patch.object(
                store, "writer_lock", side_effect=AssertionError
            ),
            mock.patch.object(store, "connect_rw", side_effect=AssertionError),
        ):
            self.assertEqual(self.stage().state, "waiting_verification")
        self.conn.execute("UPDATE review_control SET admission=0")
        with self.assertRaisesRegex(ValueError, "^rejected_validation$"):
            self.stage()

    def test_guide_display_and_application_share_cross_claim_normalization(
        self,
    ) -> None:
        refs = [self.evidence[1].id, self.evidence[0].id]
        guide = {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": "Use cache",
            "summary": {
                "text": "Prefix -----BEGIN PRIVATE KEY-----",
                "evidence_refs": refs,
            },
            "applicability": [
                {
                    "text": "U3ludGhldGljUHJpdmF0ZUtleTAxMjM0NQ==",
                    "evidence_refs": refs,
                }
            ],
            "decision_points": [
                {
                    "text": "-----END PRIVATE KEY----- suffix",
                    "evidence_refs": refs,
                }
            ],
            "pitfalls": [{"text": "Keep local.", "evidence_refs": refs}],
            "steps": [
                {
                    "key": "prepare",
                    "claim": {"text": "Prepare cache.", "evidence_refs": refs},
                    "depends_on": [],
                },
                {
                    "key": "check",
                    "claim": {"text": "Check cache.", "evidence_refs": refs},
                    "depends_on": ["prepare"],
                },
            ],
            "reason": "The owner requested a guide.",
        }
        view = self.stage(self.result([guide]))
        display = decision_candidates.show_candidate(self.conn, view.id, 65536)
        shown = json.loads(display.text)["candidate"]["result"]["changes"][0]
        expected = {
            **guide,
            "summary": {
                "text": "Prefix [redacted:secret]",
                "evidence_refs": refs,
            },
            "applicability": [
                {"text": "[redacted:secret]", "evidence_refs": refs}
            ],
            "decision_points": [
                {"text": "[redacted:secret] suffix", "evidence_refs": refs}
            ],
        }
        self.assertEqual(shown, expected)
        assert view.candidate is not None
        self.assertEqual(view.candidate.original_evidence_ids, tuple(refs))
        request = self.row(
            "SELECT * FROM review_request WHERE id=?",
            (self.context.request_id,),
        )
        self.conn.execute("BEGIN IMMEDIATE")
        knowledge_write.apply_change(
            self.conn, self.result([guide]).changes[0], request, 15.0
        )
        self.conn.commit()
        stored = json.loads(
            self.row("SELECT content_json FROM knowledge_version")[0]
        )
        self.assertEqual(
            stored, {k: v for k, v in expected.items() if k != "action"}
        )
        self.assertEqual(
            self.row("SELECT text FROM knowledge_fts")[0],
            "Prefix [redacted:secret]\n[redacted:secret]\n"
            "[redacted:secret] suffix\nKeep local.\n"
            "Prepare cache.\nCheck cache.",
        )
        changed = view.candidate.model_dump(mode="json")
        changed["result"]["changes"][0]["steps"][1]["depends_on"] = []
        self.assertNotEqual(
            decision_candidates.candidate_digest(changed), view.digest
        )

    def test_disposition_is_normalized_and_binds_historical_question(
        self,
    ) -> None:
        question = {
            "claim": "Unknown",
            "question": "Which condition applies?",
            "affected": [],
            "evidence_refs": [],
        }
        ident = self.save(self.result([], [question])).unresolved_ids[0]
        value = CandidateInput(
            request_id=self.context.request_id,
            result=self.result([self.note()]),
            disposition=QuestionDisposition(
                unresolved_id=ident,
                answer="Use token=synthetic-secret",
                affected=(),
                complete=False,
            ),
        )
        view = self.transaction(
            lambda: decision_candidates.stage_candidate(
                self.conn, self.home, value, 14.0
            )
        )
        assert (
            view.candidate is not None
            and view.candidate.disposition is not None
        )
        self.assertEqual(
            view.candidate.disposition.answer, "Use token=[redacted:secret]"
        )
        self.assertEqual(
            self.row("SELECT origin_unresolved_id FROM memory_candidate")[0],
            ident,
        )
        self.assertEqual(self.row("SELECT state FROM unresolved")[0], "open")
        empty = CandidateInput(
            request_id=self.context.request_id,
            result=self.result([], [question]),
            disposition=None,
        )
        with self.assertRaisesRegex(ValueError, "^rejected_validation$"):
            self.transaction(
                lambda: decision_candidates.stage_candidate(
                    self.conn, self.home, empty, 14.0
                )
            )

    def test_corrupt_retained_payload_never_renders_complete_display(
        self,
    ) -> None:
        view = self.stage()
        self.conn.execute("DROP TRIGGER memory_candidate_immutable")
        for payload in ("{}", '{"secret":"raw invalid text"}'):
            self.conn.execute(
                "UPDATE memory_candidate SET payload_json=?", (payload,)
            )
            before = self.projection()
            with self.assertRaisesRegex(ValueError, "^decision_invalid$"):
                decision_candidates.show_candidate(self.conn, view.id, 65536)
            self.assertEqual(self.projection(), before)
