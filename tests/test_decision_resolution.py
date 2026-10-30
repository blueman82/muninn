"""Qualify displayed unresolved-question dispositions during application."""

from __future__ import annotations

import sqlite3
from unittest import mock

from muninn import review_commit
from muninn.decision_apply import enrich_support
from muninn.decision_candidates import candidate_view, stage_candidate
from muninn.decision_models import (
    CandidateInput,
    DecisionIntent,
    DecisionView,
    QuestionDisposition,
)
from muninn.decision_proof import verify_decision
from muninn.knowledge_models import AffectedRevision
from muninn.knowledge_write import body_of
from muninn.review_commit import CommitContext
from muninn.review_models import Receipt
from muninn.review_read_budget import ReadBudget
from tests.test_decision_apply import ApplicationCase


class ResolutionCase(ApplicationCase):
    """Create unresolved questions only through ordinary checked receipts."""

    def question(
        self, *, empty: bool = False
    ) -> tuple[str, tuple[AffectedRevision, ...]]:
        """Save affected revisions and a historical unresolved receipt."""
        self.presentation_limit()
        receipt = self.save(
            self.result([self.note(), self.note(content="Use Python too.")])
        )
        refs = tuple(receipt.applied) if not empty else ()
        self.context = self.start(2, 2)
        question = {
            "claim": "Use Python.",
            "question": "Which Python rule applies?",
            "affected": [ref.model_dump() for ref in refs],
            "evidence_refs": [self.evidence[0].id],
        }
        receipt = self.save(self.result([], [question]))
        return receipt.unresolved_ids[0], refs

    def disposition(
        self,
        ident: str,
        refs: tuple[AffectedRevision, ...],
        *,
        complete: bool = True,
        replace: AffectedRevision | None = None,
    ) -> DecisionView:
        """Freeze the disclosed answer and independently chosen coverage."""
        change = self.note()
        if replace is not None:
            change.update(
                action="replace",
                knowledge_id=replace.knowledge_id,
                expected_revision=replace.revision,
            )
        return self.transaction(
            lambda: stage_candidate(
                self.conn,
                self.home,
                CandidateInput(
                    request_id=self.context.request_id,
                    result=self.result([change]),
                    disposition=QuestionDisposition(
                        unresolved_id=ident,
                        answer="Use Python.",
                        affected=refs,
                        complete=complete,
                    ),
                ),
                19.0,
            )
        )

    def save_application(self, ident: str, context: CommitContext) -> Receipt:
        """Commit the exact derived result through the normal boundary."""
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        return review_commit.commit_review(
            self.conn,
            context,
            enrich_support(candidate, self.proof.owner_evidence_id),
        )


class DecisionResolutionTests(ResolutionCase):
    """Keep unanswered historical questions open across replacements."""

    def test_full_disposition_resolves_and_preserves_historical_receipt(
        self,
    ) -> None:
        question, refs = self.question()
        history = self.governed()["review_receipt"]
        draft = self.disposition(question, refs)
        ident, context = self.application(draft)
        self.save_application(ident, context)
        self.assertEqual(
            self.row("SELECT state FROM unresolved WHERE id=?", (question,))[
                0
            ],
            "resolved",
        )
        self.assertEqual(self.governed()["review_receipt"][:2], history)
        self.assertEqual(candidate_view(self.conn, ident).state, "saved")

    def test_partial_disposition_stays_open(self) -> None:
        question, refs = self.question()
        draft = self.disposition(question, refs[:1])
        ident, context = self.application(draft)
        self.save_application(ident, context)
        self.assertEqual(
            self.row("SELECT state FROM unresolved WHERE id=?", (question,))[
                0
            ],
            "open",
        )
        self.assertEqual(candidate_view(self.conn, ident).state, "saved")

    def test_self_replacement_cannot_make_omitted_coverage_vacuous(
        self,
    ) -> None:
        question, refs = self.question()
        draft = self.disposition(question, refs[1:], replace=refs[0])
        ident, context = self.application(draft)
        self.save_application(ident, context)
        self.assertEqual(
            self.row(
                "SELECT current_revision FROM knowledge WHERE id=?",
                (refs[0].knowledge_id,),
            )[0],
            2,
        )
        self.assertEqual(
            self.row("SELECT state FROM unresolved WHERE id=?", (question,))[
                0
            ],
            "open",
        )

    def test_empty_affected_still_needs_displayed_complete_answer(
        self,
    ) -> None:
        question, refs = self.question(empty=True)
        draft = self.disposition(question, refs, complete=False)
        ident, context = self.application(draft)
        self.save_application(ident, context)
        self.assertEqual(
            self.row("SELECT state FROM unresolved WHERE id=?", (question,))[
                0
            ],
            "open",
        )

    def test_empty_affected_explicit_complete_answer_resolves(self) -> None:
        question, refs = self.question(empty=True)
        ident, context = self.application(self.disposition(question, refs))
        self.save_application(ident, context)
        self.assertEqual(
            self.row("SELECT state FROM unresolved WHERE id=?", (question,))[
                0
            ],
            "resolved",
        )

    def test_failed_fence_rolls_back_all_outputs(self) -> None:
        question, refs = self.question()
        ident, context = self.application(self.disposition(question, refs))
        faults = (
            "BEFORE INSERT ON knowledge_version "
            "BEGIN SELECT RAISE(ABORT,'knowledge'); END",
            "BEFORE INSERT ON review_receipt "
            "BEGIN SELECT RAISE(ABORT,'receipt'); END",
            "BEFORE UPDATE ON memory_candidate WHEN NEW.state='saved' "
            "BEGIN SELECT RAISE(ABORT,'candidate'); END",
            "BEFORE UPDATE ON unresolved WHEN NEW.state='resolved' "
            "BEGIN SELECT RAISE(ABORT,'resolution'); END",
            "BEFORE UPDATE ON memory_candidate WHEN NEW.state='saved' "
            "BEGIN SELECT RAISE(IGNORE); END",
        )
        for fault in faults:
            with self.subTest(fault=fault):
                before = self.governed()
                self.conn.execute(
                    f"CREATE TEMP TRIGGER application_failure {fault}"
                )
                try:
                    with self.assertRaises(
                        (sqlite3.IntegrityError, ValueError)
                    ):
                        self.save_application(ident, context)
                finally:
                    self.conn.execute("DROP TRIGGER application_failure")
                self.assertEqual(self.governed(), before)
                self.assertNotEqual(
                    candidate_view(self.conn, ident).state, "saved"
                )
                self.assertIsNone(candidate_view(self.conn, ident).receipt)


class DecisionApplicationGuardTests(ApplicationCase):
    """Reject changed proof, payload or policy at commit."""

    def test_reused_records_must_be_complete_live_and_exact(self) -> None:
        ident, context = self.application()
        intent = DecisionIntent.model_validate_json(
            self.row(
                "SELECT locator_json FROM memory_decision "
                "WHERE candidate_id=?",
                (ident,),
            )[0]
        )
        for mutation in ("partial", "owner", "original", "erased"):
            before = self.projection()

            def verify(mutation: str = mutation) -> None:
                budget = ReadBudget(self.policy.max_input_bytes)
                row = self.row(
                    "SELECT * FROM review_request WHERE id=?",
                    (context.request_id,),
                )
                records = review_commit._evidence(
                    self.conn, context, row, self.policy, budget=budget
                )
                if mutation == "partial":
                    records = records[:-1]
                else:
                    index = next(
                        i
                        for i, item in enumerate(records)
                        if item.id
                        == (
                            self.proof.owner_evidence_id
                            if mutation == "owner"
                            else self.evidence[0].id
                        )
                    )
                    update = (
                        {"state": "erased"}
                        if mutation == "erased"
                        else {"excerpt": "Changed."}
                    )
                    records = tuple(
                        (
                            record.model_copy(update=update)
                            if i == index
                            else record
                        )
                        for i, record in enumerate(records)
                    )
                verify_decision(
                    self.conn,
                    self.home,
                    intent,
                    self.policy,
                    budget,
                    from_evidence=records,
                )

            with (
                self.subTest(mutation=mutation),
                self.assertRaises(ValueError),
            ):
                self.transaction(verify)
            self.assertEqual(self.projection(), before)

    def test_fresh_original_changes_block_saved_outputs(self) -> None:
        ident, context = self.application()
        self.path.write_bytes(
            self.path.read_bytes().replace(b"Please approve", b"Please reject")
        )
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        before = self.governed()
        with self.assertRaises(ValueError):
            review_commit.commit_review(
                self.conn,
                context,
                enrich_support(candidate, self.proof.owner_evidence_id),
            )
        self.assertEqual(self.governed(), before)
        self.assertNotEqual(candidate_view(self.conn, ident).state, "saved")

    def test_active_pause_and_policy_cannot_be_overridden_by_approval(
        self,
    ) -> None:
        ident, context = self.application()
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        self.conn.execute("UPDATE review_control SET admission=0")
        before = self.governed()
        with self.assertRaisesRegex(ValueError, "rejected_validation"):
            review_commit.commit_review(
                self.conn,
                context,
                enrich_support(candidate, self.proof.owner_evidence_id),
            )
        self.assertEqual(self.governed(), before)

    def test_normalizer_drift_refuses_without_rewriting_candidate(
        self,
    ) -> None:
        ident, context = self.application()
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        body = body_of(candidate.result.changes[0])
        before = self.governed()
        with (
            mock.patch(
                "muninn.decision_apply.redacted_body",
                return_value=body.model_copy(update={"content": "Changed."}),
            ),
            self.assertRaisesRegex(ValueError, "rejected_validation"),
        ):
            review_commit.commit_review(
                self.conn,
                context,
                enrich_support(candidate, self.proof.owner_evidence_id),
            )
        self.assertEqual(self.governed(), before)
        self.assertEqual(candidate_view(self.conn, ident).candidate, candidate)

    def test_owner_support_is_required_by_the_normal_validator(self) -> None:
        draft = self.stage(
            self.result(
                [
                    self.note(
                        kind="preference", evidence_refs=[self.evidence[1].id]
                    )
                ]
            )
        )
        ident, context = self.application(draft)
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        receipt = review_commit.commit_review(
            self.conn,
            context,
            enrich_support(candidate, self.proof.owner_evidence_id),
        )
        self.assertEqual(candidate_view(self.conn, ident).receipt, receipt)
        self.assertEqual(
            {
                row[0]
                for row in self.conn.execute(
                    "SELECT evidence_id FROM knowledge_support"
                )
            },
            {self.evidence[1].id, self.proof.owner_evidence_id},
        )

    def test_proof_persistence_failure_leaves_no_orphan(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        before = self.projection()
        self.conn.execute(
            "CREATE TEMP TRIGGER refuse_display "
            "BEFORE INSERT ON review_evidence "
            "WHEN NEW.evidence_type='assistant_report' "
            "BEGIN SELECT RAISE(ABORT,'display'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.transaction(
                lambda: verify_decision(
                    self.conn,
                    self.home,
                    intent,
                    self.policy,
                    ReadBudget(self.policy.max_input_bytes),
                )
            )
        self.conn.execute("DROP TRIGGER refuse_display")
        self.assertEqual(self.projection(), before)
