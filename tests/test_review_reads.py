"""Exercise governed reads and durable admission against disposable sources."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from unittest import mock

from muninn import (
    knowledge_erase,
    review_evidence,
    review_originals,
    review_requests,
    review_rounds,
    store,
)
from muninn.knowledge_models import Scope
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext
from tests.ingest_support import rollout
from tests.test_classify import user_msg
from tests.test_review_scope import ScopeCommitCase


class ReviewReadTests(ScopeCommitCase):
    """Join real selection, commit, retrieval and conservative erasure."""

    def setUp(self) -> None:
        super().setUp()
        self.records.append(user_msg(3, "Please review the guidance."))
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        self.context = self.start()
        receipt = self.save(
            self.result([self.note(reason="Origin explanation.")])
        )
        self.ident = receipt.applied[0].knowledge_id
        self.origin_request = self.context.request_id
        self.scope = Scope(project_id=self.scope.project_id, task="reader")
        self.context = self.start(4, 4)
        for target, value in (("time.monotonic", 10.0), ("time.time", 20.0)):
            patch = mock.patch(target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def runtime(self) -> ReviewContext:
        """Reload the accumulated program-owned input union for the caller."""
        return ReviewContext(
            request_id=self.context.request_id,
            attempt_id=self.context.attempt_id,
            generation=1,
            scope=self.scope,
            policy=self.policy,
            admitted_windows=(self.window(4, 4),),
            evidence_ids=self.links(),
            round_index=1,
            deadline=100.0,
        )

    def links(self) -> tuple[str, ...]:
        """Observe committed admission through a separate store connection."""
        return tuple(
            row[0]
            for row in self.conn.execute(
                "SELECT evidence_id FROM review_input "
                "WHERE request_id=? ORDER BY evidence_id",
                (self.context.request_id,),
            )
        )

    def operation(
        self, op: str = "open", **changes: object
    ) -> review_rounds.RoundRequest:
        """Build a strict operation with no arbitrary support-ID channel."""
        data: dict[str, object] = {
            "op": op,
            "knowledge_id": self.ident,
            "revision": None,
        }
        if op == "search":
            data = {
                "op": op,
                "query": "Python",
                "scope": self.scope.model_dump(),
            }
        if op == "evidence":
            data = {
                "op": op,
                "locator": {
                    "source": self.window().source.model_dump(),
                    "line": 4,
                    "part": 1,
                    "line_sha256": self.window(4, 4).boundary_hash,
                },
                "selector": {
                    "json_pointer": "/payload/content/0/text",
                    "start": None,
                    "end": None,
                },
            }
        return review_rounds.parse_round(
            json.dumps({**data, **changes}).encode(), self.runtime()
        )

    def read(self, op: str = "open", **changes: object) -> Any:
        """Decode the exact envelope from the public read transaction."""
        return json.loads(
            review_rounds.execute_read(
                self.home, self.operation(op, **changes), self.runtime()
            )
        )

    def policy_limit(self, **limits: object) -> None:
        """Install an explicit matching policy snapshot for bound tests."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 21.0
            )
        )
        self.policy = self.policy.model_copy(update=limits)
        raw, hashed = canonical_json(self.policy), self.digest(self.policy)
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (raw, hashed),
        )
        self.context = self.start(4, 4)

    def test_search_open_and_initial_context_admit_off_window_support(
        self,
    ) -> None:
        opened = self.read()
        self.assertIsInstance(opened, dict)
        self.assertEqual(opened["id"], self.ident)
        self.assertEqual(opened["body"]["reason"], "Origin explanation.")
        self.assertEqual(self.links(), (self.evidence[0].id,))
        self.assertEqual(self.read("search"), [opened])
        prepared = json.loads(
            review_rounds.prepare_context(self.home, self.runtime())
        )
        self.assertEqual(
            [note["id"] for note in prepared["notes"]], [self.ident]
        )
        self.assertEqual(self.links(), (self.evidence[0].id,))

    def test_origin_off_claim_erasure_clears_consumer_no_change_reason(
        self,
    ) -> None:
        self.read()
        self.assertNotIn(self.evidence[1].id, self.links())
        self.context = self.context.model_copy(
            update={"evidence_ids": self.links()}
        )
        self.assertEqual(self.save(self.result([])).outcome, "no_change")
        self.transaction(
            lambda: knowledge_erase.erase_review_support(
                self.conn, (self.evidence[1].id,), 30.0
            )
        )
        self.assertIsNone(
            self.row(
                "SELECT no_change_reason FROM review_receipt "
                "WHERE request_id=?",
                (self.context.request_id,),
            )[0]
        )

    def test_selection_reuses_identity_without_reviving_erased_record(
        self,
    ) -> None:
        first = self.read("evidence")
        self.assertEqual(self.read("evidence"), first)
        ident = first["id"]
        self.conn.execute(
            "UPDATE review_evidence SET excerpt=NULL,state='erased' "
            "WHERE id=?",
            (ident,),
        )
        with self.assertRaises(ValueError):
            self.read("evidence")
        self.assertIsNone(
            self.row(
                "SELECT excerpt FROM review_evidence WHERE id=?", (ident,)
            )[0]
        )

    def test_current_attempt_policy_and_window_are_authoritative(self) -> None:
        base = self.runtime()
        for changes in (
            {"generation": 2},
            {"attempt_id": "A_" + "f" * 32},
            {"scope": Scope(project_id="other", task=None)},
            {"admitted_windows": (self.window(),)},
            {"evidence_ids": (self.evidence[0].id,)},
            {"policy": self.policy.model_copy(update={"policy_id": "forged"})},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review_rounds.execute_read(
                    self.home,
                    self.operation(),
                    base.model_copy(update=changes),
                )
        self.assertEqual(self.links(), ())

    def test_historical_withheld_and_inconsistent_support_refuse(self) -> None:
        for state in ("withheld", "erased", "retracted"):
            self.conn.execute(
                "UPDATE knowledge SET state=? WHERE id=?", (state, self.ident)
            )
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.read()
        self.conn.execute(
            "UPDATE knowledge SET state='active',current_revision=2 "
            "WHERE id=?",
            (self.ident,),
        )
        with self.assertRaises(ValueError):
            self.read(revision=1)
        self.conn.execute(
            "UPDATE knowledge SET current_revision=1 WHERE id=?", (self.ident,)
        )
        self.conn.execute(
            "DELETE FROM knowledge_support WHERE knowledge_id=?", (self.ident,)
        )
        with self.assertRaises(ValueError):
            self.read()
        self.assertEqual(self.links(), ())

    def test_oversized_response_rolls_back_admission_and_selection(
        self,
    ) -> None:
        self.policy_limit(context_bytes=20)
        before = self.row("SELECT count(*) FROM review_evidence")[0]
        for operation in ("open", "search", "evidence"):
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(ValueError, "context_limit"),
            ):
                self.read(operation)
        self.assertEqual(self.links(), ())
        self.assertEqual(
            self.row("SELECT count(*) FROM review_evidence")[0], before
        )
        self.assertFalse(self.conn.in_transaction)

    def test_admission_failure_returns_no_content_and_releases_writer(
        self,
    ) -> None:
        self.conn.execute(
            "CREATE TRIGGER reject_input BEFORE INSERT ON review_input "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.read()
        self.assertEqual(self.links(), ())
        with store.writer_lock(self.home, wait_s=0):
            self.assertFalse(self.conn.in_transaction)

    def test_one_original_budget_covers_boundary_and_support(self) -> None:
        with (
            mock.patch.object(
                review_originals, "_jsonl", wraps=review_originals._jsonl
            ) as boundary,
            mock.patch.object(
                review_evidence, "reread", wraps=review_evidence.reread
            ) as selected,
        ):
            self.read()
        accounts = [
            call.kwargs["budget"]
            for call in boundary.call_args_list + selected.call_args_list
        ]
        self.assertEqual(len(accounts), 2)
        self.assertIs(accounts[0], accounts[1])
        self.assertLess(
            accounts[0].bytes_left,
            self.policy.max_input_bytes - self.path.stat().st_size,
        )

    def test_guide_title_context_retains_transitive_origin_erasure(
        self,
    ) -> None:
        self.read()
        self.context = self.context.model_copy(
            update={"evidence_ids": self.links()}
        )
        claim = {"text": "Use Python.", "evidence_refs": [self.evidence[0].id]}
        guide = {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": "Governed title",
            "summary": claim,
            "applicability": [],
            "decision_points": [],
            "pitfalls": [],
            "reason": "Transferred explanation.",
            "steps": [{"key": "one", "claim": claim, "depends_on": []}],
        }
        guide_id = self.save(self.result([guide])).applied[0].knowledge_id
        self.policy_limit(policy_id="context-reader")
        prepared = json.loads(
            review_rounds.prepare_context(self.home, self.runtime())
        )
        self.assertEqual(prepared["guides"][0]["id"], guide_id)
        self.assertEqual(prepared["guides"][0]["title"], "Governed title")
        self.assertEqual(self.links(), (self.evidence[0].id,))
        self.context = self.context.model_copy(
            update={"evidence_ids": self.links()}
        )
        self.save(self.result([]))
        self.transaction(
            lambda: knowledge_erase.erase_review_support(
                self.conn, (self.evidence[1].id,), 30.0
            )
        )
        self.assertIsNone(
            self.row(
                "SELECT no_change_reason FROM review_receipt "
                "WHERE request_id=?",
                (self.context.request_id,),
            )[0]
        )
        self.assertEqual(
            self.row("SELECT state FROM knowledge WHERE id=?", (guide_id,))[0],
            "erased",
        )

    def test_direct_locator_hash_selector_and_window_cannot_widen(
        self,
    ) -> None:
        request = self.operation("evidence")
        assert isinstance(request, review_rounds.EvidenceRequest)
        for changes in ({"line": 2}, {"line_sha256": "f" * 64}, {"part": 99}):
            changed = request.model_copy(
                update={"locator": request.locator.model_copy(update=changes)}
            )
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review_rounds.execute_read(self.home, changed, self.runtime())
        changed = request.model_copy(
            update={
                "selector": request.selector.model_copy(
                    update={"json_pointer": "/missing"}
                )
            }
        )
        with self.assertRaises(ValueError):
            review_rounds.execute_read(self.home, changed, self.runtime())
        self.assertEqual(self.links(), ())

    def test_changed_support_original_refuses_current_knowledge(self) -> None:
        self.path.write_bytes(
            self.path.read_bytes().replace(
                b"Use Python for", b"Use Rustxx for"
            )
        )
        with self.assertRaises(ValueError):
            self.read()
        self.assertEqual(self.links(), ())

    def test_exact_response_limit_and_context_whole_item_truncation(
        self,
    ) -> None:
        raw = review_rounds.execute_read(
            self.home, self.operation(), self.runtime()
        )
        self.policy_limit(context_bytes=len(raw))
        self.assertEqual(
            review_rounds.execute_read(
                self.home, self.operation(), self.runtime()
            ),
            raw,
        )
        self.policy_limit(context_bytes=len(raw) - 1)
        with self.assertRaisesRegex(ValueError, "context_limit"):
            self.read()
        self.assertEqual(self.links(), ())
        self.policy_limit(context_bytes=100)
        prepared = json.loads(
            review_rounds.prepare_context(self.home, self.runtime())
        )
        self.assertEqual(prepared["notes"], [])
        self.assertTrue(prepared["truncated"])
        self.assertEqual(self.links(), ())

    def test_final_deadline_rolls_back_real_admission(self) -> None:
        real_link = review_rounds._link
        with mock.patch("time.monotonic", return_value=10.0) as clock:

            def expire(
                conn: sqlite3.Connection,
                context: ReviewContext,
                ids: tuple[str, ...],
            ) -> None:
                real_link(conn, context, ids)
                clock.return_value = 100.0

            with (
                mock.patch.object(review_rounds, "_link", side_effect=expire),
                self.assertRaisesRegex(ValueError, "budget_deadline"),
            ):
                self.read()
        self.assertEqual(self.links(), ())
