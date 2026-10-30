"""Erase actual decision bodies, pending locators and accepted history."""

from __future__ import annotations

from unittest import mock

from muninn import erase, review_requests, store, tombstones
from muninn.decision_candidates import candidate_view, show_candidate
from muninn.decision_proof import verify_decision
from muninn.decision_write import accept_decision, decision_view, submit_intent
from muninn.knowledge_erase import erase_review_support
from muninn.review_read_budget import ReadBudget
from tests.decision_support import DecisionCase

MARKER = 'Candidate "private" canary ERASEDECISIONBODY'


class DecisionErasureTests(DecisionCase):
    """Use physically backed synthetic originals and real decision writes."""

    def assert_erased(self, ident: str) -> None:
        """Require scrubbed views and history with valid evidence metadata."""
        view = candidate_view(self.conn, ident)
        self.assertEqual(view.state, "erased")
        self.assertIsNone(view.candidate)
        self.assertEqual(
            decision_view(self.conn, ident).code, "decision_erased"
        )
        with self.assertRaisesRegex(ValueError, "decision_erased"):
            show_candidate(self.conn, ident, 65536)
        for row in self.conn.execute(
            "SELECT state,locator_json,owner_evidence_id,display_evidence_id "
            "FROM memory_decision WHERE candidate_id=?",
            (ident,),
        ):
            self.assertEqual(tuple(row), ("erased", None, None, None))
        self.assertIsNone(
            self.row(
                "SELECT payload_json FROM memory_candidate WHERE id=?",
                (ident,),
            )[0]
        )

    def test_original_display_owner_erase_all_candidate_states(self) -> None:
        for state in (
            "waiting_verification",
            "approved_pending",
            "saved",
            "rejected",
            "blocked",
            "superseded",
            "erased",
        ):
            for role in ("original", "display", "owner"):
                with self.subTest(state=state, role=role):
                    self.doCleanups()
                    self.setUp()
                    draft = self.stage(
                        self.result([self.note(content=MARKER)])
                    )
                    intent = self.intent(draft.id)
                    proof = self.approve(intent)
                    if state == "saved":
                        self.saved_fixture(draft.id)
                    elif state == "rejected":
                        self.approve(self.intent(draft.id, action="reject"))
                    elif state == "superseded":
                        replacement = self.stage(
                            self.result(
                                [self.note(content="Independent draft")]
                            )
                        )
                        self.approve(
                            self.intent(
                                draft.id,
                                action="edit",
                                replacement_id=replacement.id,
                            )
                        )
                    elif state in ("blocked", "waiting_verification"):
                        self.conn.execute(
                            "UPDATE memory_candidate SET state=? WHERE id=?",
                            (state, draft.id),
                        )
                    elif state == "erased":
                        self.conn.execute(
                            "UPDATE memory_candidate SET state='erased',"
                            "payload_json=NULL WHERE id=?",
                            (draft.id,),
                        )
                    evidence = {
                        "original": self.evidence[0].id,
                        "display": proof.display_evidence_id,
                        "owner": proof.owner_evidence_id,
                    }[role]
                    original = self.path.read_bytes()
                    self.transaction(
                        lambda evidence=evidence: erase_review_support(
                            self.conn, (evidence,), 30.0
                        )
                    )
                    self.assert_erased(draft.id)
                    self.assertEqual(self.path.read_bytes(), original)
                    self.assertEqual(
                        self.row(
                            "SELECT count(*) FROM knowledge_version "
                            "WHERE content_json IS NOT NULL"
                        )[0],
                        0,
                    )

    def test_candidate_only_text_match_tombstones_producing_originals(
        self,
    ) -> None:
        draft = self.stage(self.result([self.note(content=MARKER)]))
        before = self.projection()
        original = self.path.read_bytes()
        dry = erase.erase(
            self.conn,
            home=self.home,
            match=MARKER,
            dry_run=True,
            env={"HOME": str(self.tmp)},
        )
        self.assertIsInstance(dry["evidence"], int)
        self.assertNotEqual(dry["evidence"], 0)
        self.assertEqual(self.projection(), before)
        self.assertFalse((self.home / "tombstone.key").exists())
        erase.erase(
            self.conn,
            home=self.home,
            match=MARKER,
            env={"HOME": str(self.tmp)},
        )
        self.assert_erased(draft.id)
        self.assertEqual(self.path.read_bytes(), original)
        self.run_ingest()
        self.assertEqual(
            self.row("SELECT count(*) FROM event WHERE line=2")[0], 0
        )

    def test_pending_locator_erasure_before_evidence_ids(self) -> None:
        for selector in ("session", "event", "match", "replay"):
            with self.subTest(selector=selector):
                self.doCleanups()
                self.setUp()
                draft = self.stage()
                intent = self.intent(draft.id)
                self.transaction(
                    lambda intent=intent: submit_intent(
                        self.conn, intent, 20.0
                    )
                )
                self.assertIsNone(
                    self.row(
                        "SELECT owner_evidence_id FROM memory_decision "
                        "WHERE candidate_id=?",
                        (draft.id,),
                    )[0]
                )
                original = self.path.read_bytes()
                locator = intent.owner_locator
                if selector == "replay":
                    tombstones.append_tombstones(
                        self.home,
                        [
                            tombstones.tombstone_row(
                                "codex",
                                "line",
                                thread_id=locator.source.thread_id,
                                line=locator.line,
                                line_sha256=locator.line_sha256,
                            )
                        ],
                    )
                    tombstones.reapply_tombstones(self.conn, self.home)
                else:
                    thread = locator.source.thread_id
                    kwargs = (
                        {"session": locator.source.thread_id}
                        if selector == "session"
                        else (
                            {
                                "event_ref": f"codex:{thread}:"
                                f"{locator.line}.1"
                            }
                            if selector == "event"
                            else {"match": locator.line_sha256}
                        )
                    )
                    erase.erase(
                        self.conn,
                        home=self.home,
                        env={"HOME": str(self.tmp)},
                        session=kwargs.get("session"),
                        event_ref=kwargs.get("event_ref"),
                        match=kwargs.get("match"),
                    )
                self.assert_erased(draft.id)
                self.assertEqual(self.path.read_bytes(), original)
                self.run_ingest()

    def test_all_accepted_history_and_late_proof_are_fenced(self) -> None:
        draft = self.stage()
        proof = self.approve(self.intent(draft.id))
        self.approve(self.intent(draft.id, action="reject"))
        history = [
            tuple(r)
            for r in self.conn.execute(
                "SELECT "
                "id,accepted_revision,accepted_at,action,candidate_digest "
                "FROM memory_decision WHERE accepted_revision IS NOT NULL"
            )
        ]
        self.transaction(
            lambda: erase_review_support(
                self.conn, (proof.owner_evidence_id,), 30.0
            )
        )
        self.assert_erased(draft.id)
        self.assertEqual(
            history,
            [
                tuple(r)
                for r in self.conn.execute(
                    "SELECT "
                    "id,accepted_revision,accepted_at,action,candidate_digest "
                    "FROM memory_decision WHERE accepted_revision IS NOT NULL"
                )
            ],
        )
        before = self.projection()
        late = self.transaction(
            lambda: accept_decision(self.conn, proof, 31.0, policy=self.policy)
        )
        self.assertEqual(late.state, "erased")
        self.assertEqual(before, self.projection())

    def test_saved_receipt_replay_cannot_restore_current_body(self) -> None:
        draft = self.stage()
        proof = self.approve(self.intent(draft.id))
        receipt = self.saved_fixture(draft.id)
        self.transaction(
            lambda: erase_review_support(
                self.conn, (proof.owner_evidence_id,), 30.0
            )
        )
        self.assert_erased(draft.id)
        with mock.patch(
            "muninn.review_originals.open_regular", side_effect=AssertionError
        ):
            self.assertEqual(self.save(self.result([])), receipt)
        self.assert_erased(draft.id)
        with self.assertRaises(ValueError):
            self.transaction(
                lambda: verify_decision(
                    self.conn,
                    self.home,
                    self.intent(draft.id),
                    self.policy,
                    ReadBudget(1_000_000),
                )
            )

    def test_saved_first_losing_edit_does_not_erase_independent_replacement(
        self,
    ) -> None:
        old = self.stage()
        proof = self.approve(self.intent(old.id))
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 19.0
            )
        )
        self.context = self.start(3, 3)
        new = self.stage(
            self.result(
                [
                    self.note(
                        content="Independent replacement",
                        evidence_refs=[self.evidence[1].id],
                    )
                ]
            )
        )
        intent = self.intent(old.id, action="edit", replacement_id=new.id)
        self.transaction(lambda: submit_intent(self.conn, intent, 20.0))
        self.saved_fixture(old.id)
        self.transaction(lambda: submit_intent(self.conn, intent, 24.0))
        before = candidate_view(self.conn, new.id)
        self.transaction(
            lambda: erase_review_support(
                self.conn, (proof.owner_evidence_id,), 30.0
            )
        )
        self.assert_erased(old.id)
        self.assertEqual(candidate_view(self.conn, new.id), before)

    def test_scrub_rolls_back_and_completed_replay_does_not_write(
        self,
    ) -> None:
        draft = self.stage()
        self.approve(self.intent(draft.id))
        before = self.projection()
        self.conn.execute("BEGIN IMMEDIATE")
        erase_review_support(self.conn, (self.evidence[0].id,), 30.0)
        self.assert_erased(draft.id)
        self.conn.rollback()
        self.assertEqual(self.projection(), before)
        erase.erase(
            self.conn,
            home=self.home,
            match="Use Python",
            env={"HOME": str(self.tmp)},
        )
        raw = store.db_path(self.home).read_bytes()
        tombstones.reapply_tombstones(self.conn, self.home)
        self.assertEqual(store.db_path(self.home).read_bytes(), raw)
