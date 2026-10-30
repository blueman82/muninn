"""Exercise exact owner-supported application through the real commit path."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

from muninn import (
    cursor_import,
    decision_proof,
    decision_requests,
    review_commit,
    review_commit_state,
    review_evidence,
    review_originals,
    review_requests,
)
from muninn.decision_apply import enrich_support
from muninn.decision_candidates import candidate_view
from muninn.decision_models import DecisionView
from muninn.review_commit import CommitContext
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceSelector,
    SourceRef,
)
from muninn.review_json import canonical_json
from muninn.review_read_budget import ReadBudget
from tests.decision_support import DecisionCase
from tests.ingest_support import line


class ApplicationCase(DecisionCase):
    """Prepare actual approved application requests without a saved stub."""

    def application(
        self, draft: DecisionView | None = None
    ) -> tuple[str, CommitContext]:
        """Admit and claim a genuine accepted candidate for direct commits."""
        draft = draft or self.stage()
        self.proof = self.approve(self.intent(draft.id))
        request = self.transaction(
            lambda: decision_requests.admit_application(
                self.conn, self.home, draft.id, 1, 21.0
            )
        )
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 21.0
            )
        )
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 22.0
            )
        )
        ids = tuple(
            row[0]
            for row in self.conn.execute(
                "SELECT evidence_id FROM review_input WHERE request_id=?",
                (request.id,),
            )
        )
        return draft.id, CommitContext(
            home=self.home,
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            evidence_ids=ids,
            now=23.0,
        )

    def presentation_limit(self) -> None:
        """Admit a fresh fixture policy for a complete larger presentation."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        self.policy = self.policy.model_copy(
            update={"context_bytes": 65536, "max_output_bytes": 65536}
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.context = self.start()

    def governed(self) -> dict[str, list[tuple[object, ...]]]:
        """Observe atomic output tables separately from refusal diagnostics."""
        return {
            name: rows
            for name, rows in self.projection().items()
            if name.startswith("knowledge")
            or name
            in {
                "review_receipt",
                "unresolved",
                "memory_decision",
                "memory_candidate_support",
                "review_evidence",
            }
        }


class DecisionApplicationTests(ApplicationCase):
    """Observe durable payloads, exact replay and all governed outputs."""

    def test_enrichment_only_appends_real_owner_support(self) -> None:
        ident, context = self.application()
        draft = candidate_view(self.conn, ident).candidate
        assert draft is not None
        before = canonical_json(draft)
        result = enrich_support(draft, self.proof.owner_evidence_id)
        expected = draft.result.model_dump(mode="json")
        expected["changes"][0]["evidence_refs"].append(
            self.proof.owner_evidence_id
        )
        self.assertEqual(result.model_dump(mode="json"), expected)
        receipt = review_commit.commit_review(self.conn, context, result)
        view = candidate_view(self.conn, ident)
        self.assertEqual((view.state, view.receipt), ("saved", receipt))
        assert view.candidate is not None
        self.assertEqual(canonical_json(view.candidate), before)
        self.assertEqual(
            self.row("SELECT count(*) FROM knowledge_support")[0], 2
        )
        self.assertEqual(
            self.row(
                "SELECT derived_digest FROM memory_candidate WHERE id=?",
                (ident,),
            )[0],
            hashlib.sha256(canonical_json(result).encode()).hexdigest(),
        )

    def test_duplicate_commit_returns_exact_receipt(self) -> None:
        ident, context = self.application()
        draft = candidate_view(self.conn, ident).candidate
        assert draft is not None
        receipt = review_commit.commit_review(
            self.conn,
            context,
            enrich_support(draft, self.proof.owner_evidence_id),
        )
        before = self.projection()
        self.path.unlink()
        with mock.patch(
            "muninn.review_commit.ReadBudget", side_effect=AssertionError
        ):
            replay = review_commit.commit_review(
                self.conn, context, self.result([])
            )
        self.assertEqual(receipt, replay)
        self.assertEqual(before, self.projection())

    def test_caller_cannot_change_approved_content(self) -> None:
        ident, context = self.application()
        with self.assertRaisesRegex(ValueError, "rejected_validation"):
            review_commit.commit_review(
                self.conn,
                context,
                self.result([self.note(content="Changed.")]),
            )
        self.assertIsNone(candidate_view(self.conn, ident).receipt)
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)

    def test_all_guide_support_fields_preserve_order_and_dependencies(
        self,
    ) -> None:
        self.presentation_limit()
        first, second = self.evidence[0].id, self.evidence[1].id
        claim = {"text": "Use Python.", "evidence_refs": [second, first]}
        guide = {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": "Python",
            "summary": claim,
            "applicability": [claim],
            "decision_points": [claim],
            "pitfalls": [claim],
            "steps": [
                {"key": "one", "claim": claim, "depends_on": []},
                {"key": "two", "claim": claim, "depends_on": ["one"]},
            ],
            "reason": "Owner requested Python.",
        }
        draft = self.stage(
            self.result([self.note(conditions=["In this project."]), guide])
        )
        ident, context = self.application(draft)
        assert draft.candidate is not None
        original = draft.candidate.result.model_dump(mode="json")
        result = enrich_support(draft.candidate, self.proof.owner_evidence_id)
        expected = draft.candidate.result.model_dump(mode="json")
        expected["changes"][0]["evidence_refs"].append(
            self.proof.owner_evidence_id
        )
        encoded = expected["changes"][1]
        for supported in [
            encoded["summary"],
            *encoded["applicability"],
            *encoded["decision_points"],
            *encoded["pitfalls"],
            *(step["claim"] for step in encoded["steps"]),
        ]:
            supported["evidence_refs"].append(self.proof.owner_evidence_id)
        self.assertEqual(result.model_dump(mode="json"), expected)
        self.assertEqual(
            draft.candidate.result.model_dump(mode="json"), original
        )
        enriched = draft.candidate.model_copy(update={"result": result})
        self.assertEqual(
            enrich_support(enriched, self.proof.owner_evidence_id), result
        )
        receipt = review_commit.commit_review(self.conn, context, result)
        self.assertEqual(len(receipt.applied), 2)
        self.assertEqual(
            self.row("SELECT count(*) FROM knowledge_claim")[0], 7
        )
        self.assertEqual(
            self.row("SELECT count(*) FROM knowledge_support")[0], 20
        )
        self.assertEqual(self.row("SELECT count(*) FROM knowledge_fts")[0], 2)
        self.assertEqual(candidate_view(self.conn, ident).state, "saved")

    def test_budget_covers_all_actual_reads_with_no_support_reread(
        self,
    ) -> None:
        ident, context = self.application()
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        result = enrich_support(candidate, self.proof.owner_evidence_id)
        exact = (
            3 * len(self.path.read_bytes())
            + len(line(self.records[1]))
            + len(line(self.records[-2]))
        )
        budgets: list[ReadBudget] = []
        for allowance in (exact - 1, exact):
            if allowance == exact:
                self.transaction(
                    lambda context=context: review_requests.retry_request(
                        self.conn, context.request_id, 24.0
                    )
                )
                attempt = self.transaction(
                    lambda context=context: review_requests.claim_attempt(
                        self.conn, context.request_id, 1, 25.0
                    )
                )
                context = context.model_copy(
                    update={"attempt_id": attempt.id, "now": 26.0}
                )
            budget = ReadBudget(allowance)
            budgets.append(budget)
            before = self.governed()
            with (
                mock.patch(
                    "muninn.review_commit.ReadBudget", return_value=budget
                ),
                mock.patch.object(
                    review_originals, "_jsonl", wraps=review_originals._jsonl
                ) as prefixes,
                mock.patch.object(
                    review_evidence, "reread", wraps=review_evidence.reread
                ) as selected,
                mock.patch.object(
                    decision_proof,
                    "_supports",
                    side_effect=AssertionError("duplicate support read"),
                ),
            ):
                if allowance == exact:
                    review_commit.commit_review(self.conn, context, result)
                else:
                    with self.assertRaisesRegex(
                        ValueError, "evidence_byte_limit"
                    ):
                        review_commit.commit_review(self.conn, context, result)
                    self.assertEqual(self.governed(), before)
                self.assertEqual(prefixes.call_count, 3)
                self.assertEqual(selected.call_count, 3)
                calls = [*prefixes.call_args_list, *selected.call_args_list]
                self.assertTrue(
                    all(call.kwargs["budget"] is budget for call in calls)
                )
            if allowance == exact:
                self.assertEqual(budget.bytes_left, 0)
        self.assertIsNot(budgets[0], budgets[1])

    def test_shared_extractions_preserve_current_bindings(self) -> None:
        self.assertIs(
            review_commit.CommitContext, review_commit_state.CommitContext
        )
        for name in (
            "EVIDENCE_ERRORS",
            "stored_receipt",
            "checked_request_policy",
        ):
            self.assertIs(
                getattr(review_commit, name),
                getattr(review_commit_state, name),
            )

    def test_native_original_shares_budget_and_closes_on_work_refusal(
        self,
    ) -> None:
        path = self.tmp / "application.sqlite"
        raw = b'{"type":1,"text":"Please use Python."}'
        with closing(sqlite3.connect(path)) as native, native:
            native.execute("CREATE TABLE cursorDiskKV(key TEXT,value BLOB)")
            native.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                [
                    (
                        "composerData:chat",
                        b'{"fullConversationHeadersOnly":[{"bubbleId":"one"}]}',
                    ),
                    ("bubbleId:chat:one", raw),
                ],
            )
        cwd = self.row("SELECT cwd FROM event LIMIT 1")[0]
        cursor_import.run(self.home, path, cwd, 0.0)
        record = review_evidence.select_evidence(
            self.home,
            EvidenceLocator(
                source=SourceRef(provider="cursor", thread_id="chat"),
                line=1,
                part=1,
                line_sha256=hashlib.sha256(raw).hexdigest(),
            ),
            EvidenceSelector(json_pointer="/text", start=None, end=None),
        )
        self.persist(record)
        self.conn.execute(
            "INSERT INTO review_input VALUES(?,?)",
            (self.context.request_id, record.id),
        )
        ident, context = self.application(
            self.stage(self.result([self.note(evidence_refs=[record.id])]))
        )
        with closing(sqlite3.connect(path)) as native, native:
            native.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                ((f"unrelated:{i}", b"{}") for i in range(40_000)),
            )
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        before = self.governed()
        seen: list[tuple[ReadBudget, sqlite3.Connection]] = []
        connect = review_originals._cursor_connection

        def observe(
            stack: ExitStack, database: Path, *, budget: ReadBudget
        ) -> sqlite3.Connection:
            conn = connect(stack, database, budget=budget)
            seen.append((budget, conn))
            return conn

        with (
            mock.patch.object(
                review_originals, "_cursor_connection", side_effect=observe
            ),
            mock.patch.object(
                review_originals, "_jsonl", wraps=review_originals._jsonl
            ) as prefixes,
            self.assertRaisesRegex(ValueError, "evidence_work_limit"),
        ):
            review_commit.commit_review(
                self.conn,
                context,
                enrich_support(candidate, self.proof.owner_evidence_id),
            )
        self.assertEqual(self.governed(), before)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], prefixes.call_args.kwargs["budget"])
        self.assertEqual(seen[0][0].sql_steps_left, 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            seen[0][1].execute("SELECT 1")
        with closing(sqlite3.connect(path, timeout=0)) as native:
            native.execute("BEGIN EXCLUSIVE")
            native.rollback()
