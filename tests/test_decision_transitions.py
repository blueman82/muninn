"""Preserve monotonic candidate history under delayed owner decisions."""

from __future__ import annotations

from muninn.decision_candidates import candidate_view
from muninn.decision_proof import verify_decision
from muninn.decision_write import accept_decision, decision_view, submit_intent
from muninn.review_read_budget import ReadBudget
from tests.decision_support import DecisionCase


class DecisionTransitionTests(DecisionCase):
    """Exercise real proof delivery rather than fabricated authorization."""

    def test_duplicate_and_late_proof_preserve_history(self) -> None:
        draft = self.stage()
        approve = self.intent(draft.id)
        proof = self.approve(approve)
        first = self.projection()
        self.transaction(
            lambda: accept_decision(self.conn, proof, 19.0, policy=self.policy)
        )
        self.assertEqual(first, self.projection())
        original = tuple(
            self.row("SELECT * FROM memory_decision WHERE accepted_revision=1")
        )
        reject = self.intent(draft.id, action="reject")
        self.approve(reject)
        view = candidate_view(self.conn, draft.id)
        self.assertEqual((view.state, view.revision), ("rejected", 2))
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT * FROM memory_decision WHERE accepted_revision=1"
                )
            ),
            original,
        )
        history = self.projection()
        self.transaction(
            lambda: accept_decision(self.conn, proof, 21.0, policy=self.policy)
        )
        self.assertEqual(history, self.projection())
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM memory_decision "
                "WHERE accepted_revision IS NOT NULL"
            )[0],
            2,
        )

    def test_pending_negative_is_not_cancellation(self) -> None:
        draft = self.stage()
        self.approve(self.intent(draft.id))
        pending = self.intent(draft.id, action="reject")
        view = self.transaction(
            lambda: submit_intent(self.conn, pending, 22.0)
        )
        self.assertEqual(
            (view.state, view.revision, view.intent_state),
            ("approved_pending", 1, "waiting_verification"),
        )
        before = self.projection()
        self.transaction(lambda: submit_intent(self.conn, pending, 23.0))
        self.assertEqual(self.projection(), before)

    def test_edit_supersedes_only_old_and_keeps_proof(self) -> None:
        old, new = self.stage(), self.stage(
            self.result([self.note(content="New draft.")])
        )
        self.approve(self.intent(old.id))
        self.approve(self.intent(old.id, action="edit", replacement_id=new.id))
        self.assertEqual(candidate_view(self.conn, old.id).state, "superseded")
        self.assertEqual(
            (
                candidate_view(self.conn, new.id).state,
                candidate_view(self.conn, new.id).revision,
            ),
            ("waiting_verification", 0),
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM memory_decision "
                "WHERE accepted_revision IS NOT NULL"
            )[0],
            2,
        )

    def test_accept_rollback_restores_all_rows(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        before = self.projection()
        self.conn.execute("BEGIN IMMEDIATE")
        submit_intent(self.conn, intent, 16.0)
        proof = verify_decision(
            self.conn,
            self.home,
            intent,
            self.policy,
            ReadBudget(self.policy.max_input_bytes),
        )
        accept_decision(self.conn, proof, 18.0, policy=self.policy)
        self.conn.rollback()
        self.assertEqual(before, self.projection())

    def test_pending_index_delay_and_read_views_preserve_store(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        self.transaction(lambda: submit_intent(self.conn, intent, 16.0))
        before = self.projection()
        for _ in range(2):
            decision_view(self.conn, draft.id)
            candidate_view(self.conn, draft.id)
            self.display(draft.id)
        self.assertEqual(before, self.projection())
        self.conn.execute(
            "UPDATE source SET status='missing' WHERE thread_id=?",
            (intent.owner_locator.source.thread_id,),
        )
        pending = self.projection()
        with self.assertRaisesRegex(ValueError, "waiting_index"):
            self.approve(intent)
        self.assertEqual(pending, self.projection())
        self.run_ingest()
        self.approve(intent)
        self.assertEqual(
            candidate_view(self.conn, draft.id).state, "approved_pending"
        )

    def test_saved_first_returns_real_receipt_without_new_history(
        self,
    ) -> None:
        old, new = self.stage(), self.stage(
            self.result([self.note(content="Independent draft.")])
        )
        approval = self.approve(self.intent(old.id))
        receipt = self.saved_fixture(old.id)
        for action in ("approve", "reject", "edit"):
            intent = self.intent(
                old.id,
                action=action,
                replacement_id=new.id if action == "edit" else None,
            )
            before = self.projection()
            view = self.transaction(
                lambda intent=intent: submit_intent(self.conn, intent, 25.0)
            )
            self.assertEqual(view.receipt, receipt)
            self.assertEqual((view.state, view.revision), ("saved", 1))
            self.assertEqual(before, self.projection())
        self.assertEqual(
            self.transaction(
                lambda: accept_decision(
                    self.conn, approval, 26.0, policy=self.policy
                )
            ).receipt,
            receipt,
        )
        self.assertEqual(candidate_view(self.conn, new.id).revision, 0)

    def test_staged_loser_remains_unaccepted_when_saved_wins(self) -> None:
        old, new = self.stage(), self.stage(
            self.result([self.note(content="Replacement draft.")])
        )
        self.approve(self.intent(old.id))
        intent = self.intent(old.id, action="edit", replacement_id=new.id)
        self.transaction(lambda: submit_intent(self.conn, intent, 20.0))
        proof = self.transaction(
            lambda: verify_decision(
                self.conn,
                self.home,
                intent,
                self.policy,
                ReadBudget(self.policy.max_input_bytes),
            )
        )
        receipt = self.saved_fixture(old.id)
        before = self.projection()
        view = self.transaction(
            lambda: accept_decision(self.conn, proof, 25.0, policy=self.policy)
        )
        self.assertEqual(
            (view.state, view.revision, view.code, view.intent_state),
            ("saved", 1, "already_saved", "blocked"),
        )
        self.assertEqual(view.receipt, receipt)
        after = self.projection()
        for table, rows in before.items():
            if table != "memory_decision":
                self.assertEqual(after[table], rows, table)
        row = self.row(
            "SELECT state,accepted_revision,owner_evidence_id,"
            "display_evidence_id,accepted_at,code FROM memory_decision "
            "WHERE action='edit'"
        )
        self.assertEqual(
            tuple(row), ("blocked", None, None, None, None, "already_saved")
        )
        self.assertEqual(
            candidate_view(self.conn, new.id).state, "waiting_verification"
        )

    def test_saved_continuation_blocks_existing_unverified_intent(
        self,
    ) -> None:
        draft = self.stage()
        self.approve(self.intent(draft.id))
        pending = self.intent(draft.id, action="reject")
        self.transaction(lambda: submit_intent(self.conn, pending, 20.0))
        receipt = self.saved_fixture(draft.id)
        view = self.transaction(
            lambda: submit_intent(self.conn, pending, 25.0)
        )
        self.assertEqual(
            (view.state, view.intent_state, view.code),
            ("saved", "blocked", "already_saved"),
        )
        self.assertEqual(view.receipt, receipt)
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM memory_decision "
                "WHERE accepted_revision IS NOT NULL"
            )[0],
            1,
        )

    def test_erased_verified_selection_cannot_accept_a_revision(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        self.transaction(lambda: submit_intent(self.conn, intent, 16.0))
        proof = self.transaction(
            lambda: verify_decision(
                self.conn,
                self.home,
                intent,
                self.policy,
                ReadBudget(self.policy.max_input_bytes),
            )
        )
        self.conn.execute(
            "UPDATE review_evidence SET state='erased',excerpt=NULL "
            "WHERE id=?",
            (proof.owner_evidence_id,),
        )
        before = self.projection()
        with self.assertRaisesRegex(ValueError, "owner_proof"):
            self.transaction(
                lambda: accept_decision(
                    self.conn, proof, 20.0, policy=self.policy
                )
            )
        self.assertEqual(before, self.projection())

    def test_failed_candidate_fence_rolls_back_new_proof_and_history(
        self,
    ) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        self.conn.execute(
            "CREATE TEMP TRIGGER ignore_candidate BEFORE UPDATE "
            "ON memory_candidate BEGIN SELECT RAISE(IGNORE); END"
        )
        before = self.projection()
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.approve(intent)
        self.assertEqual(before, self.projection())

    def test_accept_rechecks_current_fork_eligibility(self) -> None:
        draft = self.stage()
        intent = self.fork_intent(draft.id, pending=False)
        self.transaction(lambda: submit_intent(self.conn, intent, 16.0))
        proof = self.transaction(
            lambda: verify_decision(
                self.conn,
                self.home,
                intent,
                self.policy,
                ReadBudget(self.policy.max_input_bytes),
            )
        )
        self.delay_fork()
        before = self.projection()
        with self.assertRaisesRegex(ValueError, "source_unavailable"):
            self.transaction(
                lambda: accept_decision(
                    self.conn, proof, 20.0, policy=self.policy
                )
            )
        self.assertEqual(before, self.projection())

    def test_accept_uses_explicit_current_display_ceiling(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        self.transaction(lambda: submit_intent(self.conn, intent, 16.0))
        proof = self.transaction(
            lambda: verify_decision(
                self.conn,
                self.home,
                intent,
                self.policy,
                ReadBudget(self.policy.max_input_bytes),
            )
        )
        for admission in (0, 1):
            self.conn.execute(
                "UPDATE review_control SET admission=?", (admission,)
            )
            policy = self.policy.model_copy(update={"context_bytes": 32})
            before = self.projection()
            with self.assertRaisesRegex(ValueError, "decision_display_limit"):
                self.transaction(
                    lambda policy=policy: accept_decision(
                        self.conn, proof, 20.0, policy=policy
                    )
                )
            self.assertEqual(before, self.projection())
        policy = self.policy.model_copy(
            update={
                "context_bytes": len(self.display(draft.id).encode("utf-8"))
            }
        )
        view = self.transaction(
            lambda: accept_decision(self.conn, proof, 20.0, policy=policy)
        )
        self.assertEqual((view.state, view.revision), ("approved_pending", 1))
