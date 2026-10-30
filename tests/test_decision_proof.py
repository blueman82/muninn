"""Check decisions against complete disposable provider originals."""

from __future__ import annotations

import tempfile
from functools import partial
from pathlib import Path
from unittest import mock

from muninn import classify, decision_requests, erase
from muninn.decision_candidates import candidate_view
from muninn.decision_models import DecisionIntent, VerifiedDecision
from muninn.decision_proof import verify_decision
from muninn.decision_write import submit_intent
from muninn.review_evidence import select_evidence
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceSelector,
    SourceRef,
)
from muninn.review_originals import originals
from muninn.review_progress import source_row
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from tests.classify_claude_support import claude_rec
from tests.decision_support import DecisionCase
from tests.ingest_support import line, primary, rollout
from tests.test_classify import reply, user_msg


class DecisionProofTests(DecisionCase):
    """Require independent presentation, intent and provenance predicates."""

    def setUp(self) -> None:
        project = tempfile.TemporaryDirectory()
        self.addCleanup(project.cleanup)
        with mock.patch(
            "tests.test_review_requests.primary",
            return_value=primary(cwd=str(Path(project.name).resolve())),
        ):
            super().setUp()

    def test_whole_field_intent_is_independent_of_authority(self) -> None:
        draft = self.stage()
        for action in ("approve", "reject"):
            intent = self.intent(draft.id, action=action)
            proof = self.verify(intent)
            self.assertEqual(proof.action, action)
            self.assertEqual(proof.candidate_digest, draft.digest)
        for wording in (
            "Please do not approve memory {id}.",
            "Please approve memory {id} if tests pass.",
            '"Please approve memory {id}."',
            "`Please approve memory {id}.`",
            "Please approve memory {id}. Please reject memory {id}.",
            "Please approve memory D_00000000000000000000000000000000.",
            "Please approve memory {id}. Only tomorrow.",
            "<recall>Please approve memory {id}.</recall>",
        ):
            with self.subTest(wording=wording):
                intent = self.intent(
                    draft.id, wording=wording.format(id=draft.id)
                )
                before = self.projection()
                with self.assertRaises(ValueError):
                    self.verify(intent)
                self.assertEqual(self.projection(), before)
        intent = self.intent(draft.id, wording="approved")
        with self.assertRaisesRegex(ValueError, "decision_reference_required"):
            self.verify(intent)

    def test_owner_requires_whole_original_and_human_provenance(self) -> None:
        draft = self.stage()
        for updates in (
            {"role": "assistant"},
            {"channel": "analysis"},
            {"isMeta": True},
            {"isSidechain": True},
            {"isCompactSummary": True},
        ):
            with self.subTest(updates=updates):
                intent = self.intent(draft.id, owner_updates=updates)
                with self.assertRaises(ValueError):
                    self.verify(intent)
        intent = self.intent(draft.id)
        selector = intent.owner_selector.model_copy(
            update={"start": 1, "end": 5}
        )
        with self.assertRaises(ValueError):
            self.verify(intent.model_copy(update={"owner_selector": selector}))

    def test_display_binding_requires_complete_original(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        self.assertEqual(self.verify(intent).candidate_id, draft.id)
        for changed in (
            "{}",
            self.display(draft.id)[:-1],
            self.display(draft.id).replace("Append", "Remove"),
        ):
            with self.subTest(display=changed[:10]):
                intent = self.intent(draft.id, display=changed)
                with self.assertRaisesRegex(ValueError, "display_proof"):
                    self.verify(intent)
        intent = self.intent(draft.id, display_after=True)
        with self.assertRaisesRegex(ValueError, "display_proof"):
            self.verify(intent)

    def test_supplied_budget_and_no_nested_ownership(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        budget = ReadBudget(1)
        self.conn.execute("UPDATE review_control SET admission=0")
        with self.assertRaises(EvidenceLimitError) as failure:
            self.verify(intent, budget)
        self.assertEqual(failure.exception.code, "evidence_byte_limit")
        with (
            mock.patch(
                "muninn.store.writer_lock", side_effect=AssertionError("lock")
            ),
            mock.patch(
                "muninn.store.connect_ro",
                side_effect=AssertionError("connection"),
            ),
        ):
            self.verify(intent)

    def test_changed_original_and_edit_binding(self) -> None:
        old, new = self.stage(), self.stage(
            self.result([self.note(content="Other fact.")])
        )
        intent = self.intent(old.id, action="edit", replacement_id=new.id)
        proof = self.verify(intent)
        self.assertEqual(proof.replacement_digest, new.digest)
        self.path.write_bytes(
            self.path.read_bytes().replace(
                b"Please replace", b"Please approve"
            )
        )
        with self.assertRaises(ValueError):
            self.verify(intent)

    def verify(
        self, intent: DecisionIntent, budget: ReadBudget | None = None
    ) -> VerifiedDecision:
        """Verify with one operation allowance inside caller ownership."""
        return self.transaction(
            lambda: verify_decision(
                self.conn,
                self.home,
                intent,
                self.policy,
                budget or ReadBudget(self.policy.max_input_bytes),
            )
        )

    def test_split_owner_context_cannot_hide_conflicting_instruction(
        self,
    ) -> None:
        draft = self.stage()
        intent = self.intent(
            draft.id,
            owner_updates={
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Please approve memory {draft.id}.",
                    },
                    {
                        "type": "input_text",
                        "text": f"Please reject memory {draft.id}.",
                    },
                ]
            },
        )
        before = self.projection()
        with self.assertRaisesRegex(ValueError, "owner_proof"):
            self.verify(intent)
        self.assertEqual(self.projection(), before)

    def test_display_annotations_cannot_claim_presentation(self) -> None:
        draft = self.stage()
        for flag in (
            "isMeta",
            "isSidechain",
            "isCompactSummary",
            "toolUseResult",
        ):
            with self.subTest(flag=flag):
                intent = self.rewrite(
                    self.intent(draft.id), display_updates={flag: True}
                )
                with self.assertRaisesRegex(ValueError, "display_proof"):
                    self.verify(intent)

    def test_read_budget_identity_and_record_exhaustion(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        budget = ReadBudget(self.policy.max_input_bytes)
        with mock.patch(
            "muninn.decision_proof.originals", wraps=originals
        ) as reads:
            self.verify(intent, budget)
        self.assertEqual(reads.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["budget"] is budget
                for call in reads.call_args_list
            )
        )
        self.assertLess(budget.bytes_left, self.policy.max_input_bytes)
        empty = ReadBudget(self.policy.max_input_bytes)
        for _ in range(empty.records_left):
            empty.consume_record()
        with self.assertRaises(EvidenceLimitError) as failure:
            self.verify(intent, empty)
        self.assertEqual(failure.exception.code, "evidence_record_limit")

    def test_claude_primary_originals_have_the_same_proof_boundary(
        self,
    ) -> None:
        draft = self.stage()
        self.assertEqual(
            self.verify(self.claude_intent(draft.id)).candidate_digest,
            draft.digest,
        )

    def claude_intent(
        self, ident: str, *, foreign: str | None = None
    ) -> DecisionIntent:
        """Index same-project originals or one deliberate scope mismatch."""
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        project = candidate.scope.project_id
        other = str(self.tmp / "foreign project")
        rows = [
            claude_rec(
                "assistant",
                self.display(ident),
                cwd=other if foreign == "display" else project,
            ),
            claude_rec(
                "user",
                f"Please approve memory {ident}.",
                cwd=other if foreign == "owner" else project,
            ),
        ]
        self.write("project/sess-1.jsonl", rows, "claude-projects")
        self.run_ingest()
        source = SourceRef(provider="claude", thread_id="sess-1")
        selector = EvidenceSelector(
            json_pointer="/message/content", start=None, end=None
        )
        return DecisionIntent(
            candidate_id=ident,
            action="approve",
            replacement_id=None,
            owner_locator=EvidenceLocator(
                source=source,
                line=2,
                part=1,
                line_sha256=classify.record_hash(line(rows[1])),
            ),
            display_locator=EvidenceLocator(
                source=source,
                line=1,
                part=1,
                line_sha256=classify.record_hash(line(rows[0])),
            ),
            owner_selector=selector,
            display_selector=selector,
        )

    def test_foreign_claude_project_cannot_accept_or_apply(self) -> None:
        for foreign in ("owner", "display"):
            with self.subTest(foreign=foreign):
                draft = self.stage()
                intent = self.claude_intent(draft.id, foreign=foreign)
                before = self.projection()
                with self.assertRaisesRegex(ValueError, "^rejected_scope$"):
                    self.approve(intent)
                self.assertEqual(self.projection(), before)
                self.transaction(
                    partial(submit_intent, self.conn, intent, 20.0)
                )
                view = decision_requests.request_application(
                    self.home, draft.id, 21.0
                )
                self.assertEqual(
                    (view.code, view.revision), ("rejected_scope", 0)
                )
                self.assertIsNone(view.receipt)
                for name, rows in before.items():
                    if name.startswith("knowledge") or name in (
                        "review_evidence",
                        "review_request",
                        "review_receipt",
                    ):
                        self.assertEqual(self.projection()[name], rows, name)
                decision = self.row(
                    "SELECT accepted_revision,owner_evidence_id,"
                    "display_evidence_id FROM memory_decision "
                    "WHERE candidate_id=?",
                    (draft.id,),
                )
                self.assertEqual(tuple(decision), (None, None, None))
                self.assertIsNone(
                    self.row(
                        "SELECT application_request_id "
                        "FROM memory_candidate WHERE id=?",
                        (draft.id,),
                    )[0]
                )

    def test_authority_alone_does_not_establish_affirmative_intent(
        self,
    ) -> None:
        draft = self.stage()
        intent = self.intent(
            draft.id, wording=f"Please do not approve memory {draft.id}."
        )
        owner = select_evidence(
            self.home, intent.owner_locator, intent.owner_selector
        )
        self.assertEqual(
            (owner.authority, owner.evidence_type),
            ("direct_owner", "owner_statement"),
        )
        with self.assertRaisesRegex(ValueError, "owner_intent"):
            self.verify(intent)

    def test_erased_physical_source_cannot_supply_owner_proof(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        erase.erase(
            self.conn,
            home=self.home,
            event_ref=f"codex:{intent.owner_locator.source.thread_id}:{intent.owner_locator.line}.1",
        )
        before = self.projection()
        with self.assertRaises(ValueError):
            self.verify(intent)
        self.assertEqual(before, self.projection())

    def test_unverified_fork_proof_refuses_then_verified_suffix_succeeds(
        self,
    ) -> None:
        draft = self.stage()
        intent = self.fork_intent(draft.id)
        self.assertEqual(
            self.need_source("pending-fork")["replay_mode"], "unverified"
        )
        with self.assertRaisesRegex(ValueError, "source_unavailable"):
            source_row(self.conn, intent.owner_locator.source)
        before = self.projection()
        with self.assertRaises(ValueError):
            self.verify(intent)
        self.assertEqual(before, self.projection())
        self.run_ingest()
        self.assertNotEqual(
            self.need_source("pending-fork")["replay_mode"], "unverified"
        )
        self.fork_rows.extend(
            [
                user_msg(7, "Use the current project scope."),
                reply(8, self.display(draft.id)),
                user_msg(9, f"Please approve memory {draft.id}."),
            ]
        )
        self.write(rollout("pending-fork"), self.fork_rows)
        self.run_ingest()
        fresh = intent.model_copy(
            update={
                "display_locator": intent.display_locator.model_copy(
                    update={
                        "line": 5,
                        "line_sha256": classify.record_hash(
                            line(self.fork_rows[4])
                        ),
                    }
                ),
                "owner_locator": intent.owner_locator.model_copy(
                    update={
                        "line": 6,
                        "line_sha256": classify.record_hash(
                            line(self.fork_rows[5])
                        ),
                    }
                ),
            }
        )
        self.assertEqual(self.verify(fresh).candidate_id, draft.id)

    def test_complete_display_uses_context_bytes_even_when_admission_disabled(
        self,
    ) -> None:
        draft = self.stage(
            self.result([self.note(content="Use Python. \u00e9")])
        )
        intent = self.intent(draft.id)
        size = len(self.display(draft.id).encode("utf-8"))
        self.assertGreater(size, len(self.display(draft.id)))
        for admission in (0, 1):
            self.conn.execute(
                "UPDATE review_control SET admission=?", (admission,)
            )
            for ceiling in (32, size - 1):
                with self.subTest(admission=admission, ceiling=ceiling):
                    self.policy = self.policy.model_copy(
                        update={"context_bytes": ceiling}
                    )
                    before = self.projection()
                    with self.assertRaisesRegex(
                        ValueError, "decision_display_limit"
                    ):
                        self.verify(intent)
                    self.assertEqual(before, self.projection())
            self.policy = self.policy.model_copy(
                update={"context_bytes": size}
            )
            self.assertEqual(self.verify(intent).candidate_id, draft.id)
