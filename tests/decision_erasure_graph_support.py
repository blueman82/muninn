"""Build physically backed disposition chains through actual normal commits."""

from __future__ import annotations

from dataclasses import dataclass

from muninn import (
    classify,
    erase,
    query,
    review_evidence,
    review_requests,
    tombstones,
)
from muninn.decision_candidates import candidate_view, stage_candidate
from muninn.decision_models import (
    CandidateInput,
    DecisionIntent,
    DecisionView,
    QuestionDisposition,
    VerifiedDecision,
)
from muninn.decision_write import submit_intent
from muninn.knowledge_models import AffectedRevision
from muninn.review_commit import CommitContext
from muninn.review_evidence_models import EvidenceWindow, SourceRef
from muninn.review_models import Receipt
from tests.ingest_support import TID, line, primary, rollout
from tests.test_classify import user_msg
from tests.test_decision_resolution import ResolutionCase

ANSWER = 'Disposition-only "private" answer ERASEDISPOSITIONCHAIN'


@dataclass(frozen=True)
class DecisionGraph:
    """Keep expected identities returned by actual saved operations."""

    candidates: tuple[str, ...]
    questions: tuple[str, ...]
    versions: tuple[AffectedRevision, ...]
    first_intent: DecisionIntent
    pending_intent: DecisionIntent
    proof_ids: tuple[str, ...]
    unselected_original_ids: tuple[str, ...]
    first_proof: VerifiedDecision
    first_context: CommitContext
    first_receipt: Receipt
    original_receipt: tuple[object, ...]
    unrelated_question: str
    unrelated_candidate: str


class DecisionGraphCase(ResolutionCase):
    """Create explicit graph dependencies without fabricated relation rows."""

    def fresh_request(self, text: str) -> str:
        """Select a new owner field and claim its ordinary window."""
        self.records.append(user_msg(5, text))
        number = len(self.records)
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        records = review_evidence.initial_evidence(
            self.home, self.window(number, number), self.policy.max_input_bytes
        ).excerpts
        self.assertEqual(len(records), 1)
        self.persist(records[0])
        self.evidence += records
        self.context = self.start(number, number)
        return records[0].id

    def draft_answer(
        self,
        question: str,
        affected: tuple[AffectedRevision, ...],
        evidence: str,
        answer: str,
        *,
        content: str | None = None,
    ) -> DecisionView:
        """Stage a real nonempty disposition on the current preparation R."""
        return self.transaction(
            lambda: stage_candidate(
                self.conn,
                self.home,
                CandidateInput(
                    request_id=self.context.request_id,
                    result=self.result(
                        [
                            self.note(
                                content=content or answer,
                                evidence_refs=[evidence],
                            )
                        ]
                    ),
                    disposition=QuestionDisposition(
                        unresolved_id=question,
                        answer=answer,
                        affected=affected,
                        complete=True,
                    ),
                ),
                19.0,
            )
        )

    def independent(self) -> tuple[str, str]:
        """Save a question and stage a draft on a separate physical session."""
        rows = primary("unrelated-owner", cwd=self.scope.project_id)
        self.write(rollout("unrelated-owner"), rows)
        self.run_ingest()
        window = EvidenceWindow(
            source=SourceRef(provider="codex", thread_id="unrelated-owner"),
            from_line=2,
            through_line=2,
            boundary_hash=classify.record_hash(line(rows[1])),
        )
        records = review_evidence.initial_evidence(
            self.home, window, self.policy.max_input_bytes
        ).excerpts
        self.assertEqual(len(records), 1)
        self.persist(records[0])
        request = self.enqueue(window)
        self.conn.execute(
            "INSERT INTO review_input VALUES(?,?)", (request.id, records[0].id)
        )
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.context = CommitContext(
            home=self.home,
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            evidence_ids=(records[0].id,),
            now=13.0,
        )
        question = self.save(
            self.result(
                [],
                [
                    {
                        "claim": "Independent claim",
                        "question": "Independent question?",
                        "affected": [],
                        "evidence_refs": [records[0].id],
                    }
                ],
            )
        ).unresolved_ids[0]
        candidate = self.stage(
            self.result(
                [
                    self.note(
                        content="Independent replacement",
                        evidence_refs=[records[0].id],
                    )
                ]
            )
        )
        return question, candidate.id

    def dependent_question(
        self, affected: tuple[AffectedRevision, ...], label: str
    ) -> tuple[str, str]:
        """Save a normal question about the prior actual applied revision."""
        support = self.fresh_request(
            "Which " + label + " rule needs follow-up?"
        )
        receipt = self.save(
            self.result(
                [],
                [
                    {
                        "claim": "Dependent " + label + " question",
                        "question": "How does the " + label + " rule apply?",
                        "affected": [item.model_dump() for item in affected],
                        "evidence_refs": [support],
                    }
                ],
            )
        )
        return receipt.unresolved_ids[0], support

    def graph(self) -> DecisionGraph:
        """Apply two disclosed answers with a dependent question."""
        origin, previous = self.question()
        original_receipt = tuple(
            self.row(
                "SELECT r.* FROM review_receipt r JOIN unresolved u ON "
                "u.request_id=r.request_id "
                "WHERE u.id=?",
                (origin,),
            )
        )
        original_support = self.fresh_request(
            "Prepare the first disclosed answer."
        )
        first = self.draft_answer(origin, previous, original_support, ANSWER)
        preparation = self.context
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, preparation.request_id, 20.0
            )
        )
        unrelated_question, independent = self.independent()
        self.context = preparation
        ident, application = self.application(first)
        proof = self.proof
        first_intent = DecisionIntent.model_validate_json(
            self.row(
                "SELECT locator_json FROM memory_decision WHERE "
                "candidate_id=?",
                (ident,),
            )[0]
        )
        losing = self.intent(ident, action="edit", replacement_id=independent)
        self.transaction(lambda: submit_intent(self.conn, losing, 22.0))
        first_receipt = self.save_application(ident, application)
        self.transaction(lambda: submit_intent(self.conn, losing, 24.0))
        second_question, second_support = self.dependent_question(
            tuple(first_receipt.applied), "first"
        )
        second = self.draft_answer(
            second_question,
            tuple(first_receipt.applied),
            second_support,
            "Complete second disclosed answer.",
        )
        second_id, second_application = self.application(second)
        second_receipt = self.save_application(second_id, second_application)
        third_question, third_support = self.dependent_question(
            tuple(second_receipt.applied), "second"
        )
        third = self.draft_answer(
            third_question,
            tuple(second_receipt.applied),
            third_support,
            "A waiting third answer.",
        )
        pending = self.intent(third.id)
        self.transaction(lambda: submit_intent(self.conn, pending, 25.0))
        self.assertNotEqual(first_receipt.request_id, original_receipt[0])
        self.assertNotEqual(preparation.request_id, original_receipt[0])
        self.assertNotEqual(first_receipt.request_id, preparation.request_id)
        return DecisionGraph(
            (first.id, second.id, third.id),
            (origin, second_question, third_question),
            previous
            + tuple(first_receipt.applied)
            + tuple(second_receipt.applied),
            first_intent,
            pending,
            tuple(
                r[0]
                for r in self.conn.execute(
                    "SELECT owner_evidence_id FROM memory_decision WHERE "
                    "accepted_revision IS NOT NULL "
                    "UNION SELECT display_evidence_id FROM "
                    "memory_decision WHERE accepted_revision IS NOT NULL"
                )
            ),
            (second_support, third_support),
            proof,
            application,
            first_receipt,
            original_receipt,
            unrelated_question,
            independent,
        )

    def select_graph(self, graph: DecisionGraph, selection: str) -> None:
        """Use a public selector or caller-owned replay."""
        if selection == "replay":
            locator = graph.first_intent.owner_locator
            tombstones.append_tombstones(
                self.home,
                [
                    tombstones.tombstone_row(
                        "codex",
                        "line",
                        thread_id=TID,
                        line=locator.line,
                        line_sha256=locator.line_sha256,
                    )
                ],
            )
            tombstones.reapply_tombstones(self.conn, self.home)
        else:
            candidate = candidate_view(
                self.conn, graph.candidates[0]
            ).candidate
            assert candidate is not None
            number = {
                "original": self.row(
                    "SELECT line FROM review_evidence WHERE id=?",
                    (candidate.original_evidence_ids[0],),
                )[0],
                "owner": graph.first_intent.owner_locator.line,
                "display": graph.first_intent.display_locator.line,
            }.get(selection)
            erase.erase(
                self.conn,
                home=self.home,
                session=TID if selection == "session" else None,
                event_ref=(f"codex:{TID}:{number}.1" if number else None),
                match=ANSWER if selection == "text" else None,
                env={"HOME": str(self.tmp)},
            )

    def assert_original_questions_kept(self, graph: DecisionGraph) -> None:
        """Keep unselected question fields distinct from erased drafts."""
        for evidence in graph.unselected_original_ids:
            row = self.row(
                "SELECT state,excerpt,thread_id,line,part FROM "
                "review_evidence WHERE id=?",
                (evidence,),
            )
            self.assertEqual(row[0], "live")
            self.assertIsNotNone(row[1])
            reference = f"codex:{row[2]}:{row[3]}.{row[4]}"
            self.assertIsNone(
                query.open_event(
                    self.conn, reference, roots=self.roots, context=0
                ).get("error")
            )
