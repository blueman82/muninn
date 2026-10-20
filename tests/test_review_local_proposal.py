"""Check local proposal admission, fencing and conservative call history."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest import mock

from muninn import (
    review_budget,
    review_evidence,
    review_originals,
    review_requests,
    store,
)
from muninn.review_json import canonical_json
from muninn.review_models import AttemptView, ReviewContext
from tests.ingest_support import TID, line
from tests.test_review_commit import CommitCase


class LocalProposalTests(CommitCase):
    """Use checked requests and synthetic originals in disposable stores."""

    def claim(
        self, generation: int = 1, *, request_id: str | None = None
    ) -> AttemptView:
        """Claim the fixture request inside its caller-owned transaction."""
        return self.transaction(
            lambda: review_requests.claim_proposal(
                self.conn,
                request_id or self.context.request_id,
                generation,
                15.0,
            )
        )

    def state(self) -> tuple[str, ...]:
        """Snapshot all durable fixture state for exact rollback checks."""
        return tuple(self.conn.iterdump())

    def runtime(self, attempt: str, round_index: int = 1) -> ReviewContext:
        """Build a program-owned context for the unchanged request policy."""
        return ReviewContext(
            request_id=self.context.request_id,
            attempt_id=attempt,
            generation=1,
            scope=self.scope,
            policy=self.policy,
            admitted_windows=(self.window(),),
            evidence_ids=self.context.evidence_ids,
            round_index=round_index,
            deadline=72.0,
        )

    def install_policy(self) -> None:
        """Install explicit synthetic policy changes before tested claims."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        values = (canonical_json(self.policy), self.digest(self.policy))
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?", values
        )
        request = self.enqueue()
        self.conn.executemany(
            "INSERT INTO review_input VALUES(?,?)",
            [(request.id, ident) for ident in self.context.evidence_ids],
        )
        self.context = self.context.model_copy(
            update={"request_id": request.id}
        )

    def test_current_worker_fenced_and_local_attempt_commits(self) -> None:
        old = dict(self.row("SELECT * FROM review_attempt"))
        request = dict(self.row("SELECT * FROM review_request"))
        control = tuple(self.row("SELECT * FROM review_control"))
        with mock.patch.object(
            store, "writer_lock", side_effect=AssertionError("nested lock")
        ):
            local = self.claim()
        self.assertNotEqual(local.id, old["id"])
        self.assertIsNone(local.requested)
        self.assertEqual(local.observed, review_requests.UNKNOWN_CONFIG)
        self.assertEqual(
            (local.generation, local.outcome), (1, "operator_retry")
        )
        self.assertEqual(
            tuple(self.row("SELECT * FROM review_control")), control
        )
        self.assertEqual(
            dict(
                self.row(
                    "SELECT * FROM review_attempt WHERE id=?", (old["id"],)
                )
            ),
            {
                **old,
                "ended_at": 15.0,
                "progress_at": 15.0,
                "outcome": "operator_retry",
            },
        )
        self.assertEqual(
            dict(self.row("SELECT * FROM review_request")),
            {**request, "origin": "proposal", "current_attempt_id": local.id},
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.save(self.result([]))
        self.context = self.context.model_copy(
            update={"attempt_id": local.id, "now": 16.0}
        )
        self.assertFalse(self.conn.in_transaction)
        receipt = self.save(self.result([]))
        self.assertEqual(receipt.attempt_id, local.id)

    def test_receipt_first_survives_mutable_guards_without_reads(self) -> None:
        receipt = self.save(self.result([]))
        self.conn.execute("UPDATE review_control SET admission=0,generation=2")
        self.path.unlink()
        before = self.state()
        with mock.patch.object(
            review_originals, "_jsonl", side_effect=AssertionError("read")
        ):
            for generation in (1, 2):
                self.assertEqual(self.claim(generation).id, receipt.attempt_id)
                self.assertEqual(self.save(self.result([])), receipt)
        self.assertEqual(self.state(), before)
        self.assertIsNotNone(self.claim().requested)
        self.assertEqual(
            self.row("SELECT origin FROM review_request")[0], "review"
        )

    def test_new_pending_proposal_and_explicit_terminal_retries(self) -> None:
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 13.0
            )
        )
        request = self.enqueue(self.window(2, 2))
        first = self.claim(request_id=request.id)
        self.assertIsNone(first.requested)
        for state in ("waiting", "cancelled", "failed", "running"):
            with self.subTest(state=state):
                self.conn.execute(
                    "UPDATE review_request SET state=?,retry_at=1000.0 "
                    "WHERE id=?",
                    (state, request.id),
                )
                local = self.claim(request_id=request.id)
                self.assertIsNone(local.requested)
                self.assertEqual(local.outcome, "operator_retry")
                self.assertEqual(
                    self.row(
                        "SELECT state,retry_at,cancel_code "
                        "FROM review_request WHERE id=?",
                        (request.id,),
                    )[:],
                    ("running", None, None),
                )
        self.assertGreater(
            self.row(
                "SELECT count(*) FROM review_attempt WHERE request_id=?",
                (request.id,),
            )[0],
            self.policy.max_attempts,
        )

    def test_current_generation_and_caller_transaction_required(self) -> None:
        before = self.state()
        with self.assertRaisesRegex(ValueError, "caller_transaction_required"):
            review_requests.claim_proposal(
                self.conn, self.context.request_id, 1, 15.0
            )
        for generation in (0, 2, True):
            with self.subTest(generation=generation):
                with self.assertRaisesRegex(ValueError, "rejected_fenced"):
                    self.claim(generation)
                self.assertEqual(self.state(), before)

    def test_concurrency_refusal_does_not_fence_another_request(self) -> None:
        other = self.enqueue(self.window(2, 2))
        before = self.state()
        with self.assertRaisesRegex(ValueError, "review_concurrency"):
            self.claim(request_id=other.id)
        self.assertEqual(self.state(), before)
        local = self.claim()
        self.assertEqual(local.request_id, self.context.request_id)
        self.assertEqual(
            self.row(
                "SELECT state FROM review_request WHERE id=?", (other.id,)
            )[0],
            "pending",
        )

    def test_cross_request_current_identity_cannot_be_fenced(self) -> None:
        other = self.enqueue(self.window(2, 2))
        self.conn.execute(
            "UPDATE review_request SET current_attempt_id=? WHERE id=?",
            (self.context.attempt_id, other.id),
        )
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        before = self.state()
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.claim(request_id=other.id)
        self.assertEqual(self.state(), before)

    def test_unknown_calls_keep_charges_clocks_metadata_and_caps(self) -> None:
        self.policy = self.policy.model_copy(
            update={
                "budget": self.policy.budget.model_copy(
                    update={
                        "max_cost_decimal": "2.00",
                        "conservative_call_cost_decimal": "0.75",
                        "currency": "EUR",
                    }
                )
            }
        )
        self.install_policy()
        worker = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, self.context.request_id, 1, 12.0
            )
        )
        self.context = self.context.model_copy(
            update={"attempt_id": worker.id}
        )
        for index in (1, 2):
            self.transaction(
                lambda i=index: review_budget.reserve_call(
                    self.conn,
                    self.runtime(self.context.attempt_id, i),
                    12.0 + i,
                )
            )
            self.conn.execute("UPDATE review_call SET state='completed'")
        self.conn.execute(
            "UPDATE review_call SET state='reserved' WHERE round_index=1"
        )
        self.conn.execute(
            "UPDATE review_call SET state='launched',launched_at=14.5 "
            "WHERE round_index=2"
        )
        calls = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM review_call ORDER BY round_index"
            )
        ]
        local = self.claim()
        self.assertEqual(
            [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM review_call ORDER BY round_index"
                )
            ],
            [
                {
                    **row,
                    "state": "unknown",
                    "ended_at": 15.0,
                    "outcome": "operator_retry",
                }
                for row in calls
            ],
        )
        self.assertIsNone(
            review_budget.aggregate_usage(
                self.conn, self.policy.budget.account_id
            ).cost_decimal
        )
        for attempt, error in (
            (self.context.attempt_id, "rejected_fenced"),
            (local.id, "budget_call_limit"),
        ):
            with self.assertRaisesRegex(ValueError, error):
                self.transaction(
                    lambda ident=attempt: review_budget.reserve_call(
                        self.conn, self.runtime(ident), 16.0
                    )
                )
        self.assertEqual(
            self.row("SELECT count(*),min(reserved_at) FROM review_call")[:],
            (2, 13.0),
        )

    def test_policy_source_erasure_and_authority_refuse_before_fencing(
        self,
    ) -> None:
        faults = (
            (
                "UPDATE review_control SET admission=0",
                "review_policy_required",
            ),
            (
                "UPDATE review_control SET policy_digest='changed'",
                "review_policy_required",
            ),
            ("UPDATE source SET status='missing'", "source_unavailable"),
            (
                "UPDATE review_evidence SET state='erased',excerpt=NULL",
                "evidence_unavailable",
            ),
            (
                "UPDATE review_evidence SET authority='reported' "
                "WHERE authority='direct_owner'",
                "evidence_changed",
            ),
        )
        for sql, code in faults:
            with self.subTest(code=code):
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.conn.execute(sql)
                    before = self.state()
                    with self.assertRaisesRegex(ValueError, code):
                        review_requests.claim_proposal(
                            self.conn, self.context.request_id, 1, 15.0
                        )
                    self.assertEqual(self.state(), before)
                finally:
                    self.conn.execute("ROLLBACK")
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',?,2,?,14.0)",
            (TID, self.window(2, 2).boundary_hash),
        )
        before = self.state()
        with self.assertRaisesRegex(ValueError, "^erasure_pending$"):
            self.claim()
        self.assertEqual(self.state(), before)

    def test_changed_original_and_failed_insert_roll_back_fence(self) -> None:
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b"Use Python", b"Use Kotlin"))
        before = self.state()
        with self.assertRaises(ValueError):
            self.claim()
        self.assertEqual(self.state(), before)
        self.path.write_bytes(original)
        self.conn.execute(
            "CREATE TEMP TRIGGER refuse_local BEFORE INSERT ON review_attempt "
            "WHEN NEW.requested_json IS NULL "
            "BEGIN SELECT RAISE(ABORT,'refused'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "refused"):
            self.claim()
        self.assertEqual(self.state(), before)

    def test_window_and_all_selected_reads_share_exact_allowance(self) -> None:
        exact = len(self.path.read_bytes()) + sum(
            len(line(self.records[e.line - 1])) for e in self.evidence
        )
        for cap in (exact - 1, exact):
            self.policy = self.policy.model_copy(
                update={"max_input_bytes": cap}
            )
            self.install_policy()
            before = self.state()
            with (
                mock.patch.object(
                    review_originals, "_jsonl", wraps=review_originals._jsonl
                ) as prefix,
                mock.patch.object(
                    review_evidence, "reread", wraps=review_evidence.reread
                ) as selected,
            ):
                if cap < exact:
                    with self.assertRaisesRegex(ValueError, "evidence_limit"):
                        self.claim()
                    self.assertEqual(self.state(), before)
                else:
                    self.claim()
            budget = prefix.call_args.kwargs["budget"]
            self.assertEqual(selected.call_count, len(self.evidence))
            self.assertTrue(
                all(
                    call.kwargs["budget"] is budget
                    for call in selected.call_args_list
                )
            )
            if cap == exact:
                self.assertEqual(
                    (budget.bytes_left, budget.records_left),
                    (0, 4096 - len(self.records) - len(self.evidence)),
                )

    def test_writer_claim_visible_only_after_caller_commit(self) -> None:
        with closing(
            store.connect_rw(store.db_path(self.home), fullfsync=False)
        ) as other:
            other.execute("PRAGMA busy_timeout=0")
            self.conn.execute("BEGIN IMMEDIATE")
            local = review_requests.claim_proposal(
                self.conn, self.context.request_id, 1, 15.0
            )
            self.assertNotEqual(
                other.execute(
                    "SELECT current_attempt_id FROM review_request"
                ).fetchone()[0],
                local.id,
            )
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                other.execute("BEGIN IMMEDIATE")
            self.conn.execute("COMMIT")
            self.assertEqual(
                other.execute(
                    "SELECT current_attempt_id FROM review_request"
                ).fetchone()[0],
                local.id,
            )
