"""Check durable source refusals before creating a review attempt."""

from __future__ import annotations

import sqlite3
from unittest import mock

from muninn import (
    review_budget,
    review_claims,
    review_evidence,
    review_originals,
    review_requests,
    store,
)
from muninn.knowledge_models import Scope
from muninn.review_evidence_models import SourceRef
from muninn.review_json import canonical_json
from muninn.review_models import AttemptView, RequestView, ReviewContext
from tests.ingest_support import line, primary, rollout
from tests.test_review_requests import RequestCase


class ClaimRefusalTests(RequestCase):
    """Exercise caller-owned transactions against disposable originals."""

    def setUp(self) -> None:
        super().setUp()
        self.request = self.enqueue()

    def claim(self, generation: int = 1) -> AttemptView | RequestView:
        """Run the guarded claim inside its caller transaction."""
        return self.transaction(lambda: self.claim_current(generation))

    def claim_current(self, generation: int = 1) -> AttemptView | RequestView:
        """Call admission without opening or ending a caller transaction."""
        return review_requests.claim_or_refuse_attempt(
            self.conn, self.request.id, generation, 15.0
        )

    def state(self, *, requests: bool = True) -> tuple[str, ...]:
        """Snapshot all fixture rows, optionally excluding request rows."""
        return tuple(
            row
            for row in self.conn.iterdump()
            if requests or not row.startswith('INSERT INTO "review_request"')
        )

    def refused(self) -> None:
        """Assert that refusal changes only its three request columns."""
        before = dict(review_requests.request_row(self.conn, self.request.id))
        unchanged = self.state(requests=False)
        result = self.claim()
        assert isinstance(result, RequestView)
        self.assertEqual(result.state, "failed")
        self.assertEqual(
            dict(review_requests.request_row(self.conn, self.request.id)),
            {
                **before,
                "state": "failed",
                "retry_at": None,
                "current_attempt_id": None,
            },
        )
        self.assertEqual(self.state(requests=False), unchanged)

    def worker(
        self, request_id: str | None = None, now: float = 12.0
    ) -> AttemptView:
        """Claim a normal fixture worker without changing its contract."""
        return self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request_id or self.request.id, 1, now
            )
        )

    def fail_worker(self, attempt: AttemptView, code: str, now: float) -> None:
        """End a fixture attempt through the real failure transition."""
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, attempt.id, code, now
            )
        )

    def guard(self, code: str, generation: int = 1) -> None:
        """Assert a guard rejects before source reads or durable mutation."""
        before = self.state()
        with (
            mock.patch.object(
                review_claims,
                "check_window",
                side_effect=AssertionError("read"),
            ),
            self.assertRaisesRegex(ValueError, code),
        ):
            self.claim_current(generation)
        self.assertEqual(self.state(), before)

    def test_ordinary_claim_still_propagates_missing_original(self) -> None:
        self.path.unlink()
        before = self.state()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "original_unavailable"):
                self.worker(now=15.0)
            self.assertEqual(self.state(), before)

    def test_missing_original_refusal_survives_restart_and_requires_retry(
        self,
    ) -> None:
        original = self.path.read_bytes()
        self.path.unlink()
        self.refused()
        self.conn.close()
        self.conn = store.connect_rw(store.db_path(self.home), fullfsync=False)
        self.addCleanup(self.conn.close)
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_request").fetchone()[
                0
            ],
            "failed",
        )
        for table in ("review_attempt", "review_call", "review_receipt"):
            self.assertEqual(
                self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[
                    0
                ],
                0,
            )
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "request_not_ready"):
            self.claim()
        self.transaction(
            lambda: review_requests.retry_request(
                self.conn, self.request.id, 16.0
            )
        )
        attempt = self.claim()
        assert isinstance(attempt, AttemptView)
        self.assertEqual(attempt.outcome, "operator_retry")

    def test_later_healthy_request_remains_claimable(self) -> None:
        tid = "0199aaaa-bbbb-4ccc-8ddd-ffffffffffff"
        self.write(rollout(tid), primary(tid))
        self.run_ingest()
        later = self.transaction(
            lambda: review_requests.enqueue_review(
                self.conn,
                self.window().model_copy(
                    update={
                        "source": SourceRef(provider="codex", thread_id=tid)
                    }
                ),
                self.scope,
                self.policy,
                "explicit",
                12.0,
            )
        )
        self.path.unlink()
        self.refused()
        self.request = later
        self.assertIsInstance(self.claim(), AttemptView)

    def test_waiting_and_operator_histories_keep_unknown_charges(self) -> None:
        attempt = self.worker()
        context = ReviewContext(
            request_id=self.request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=self.scope,
            policy=self.policy,
            admitted_windows=(self.window(),),
            evidence_ids=(),
            round_index=1,
            deadline=72.0,
        )
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 12.5)
        )
        self.fail_worker(attempt, "unavailable", 13.0)
        original = self.path.read_bytes()
        self.path.unlink()
        self.conn.execute(
            "UPDATE review_request SET current_attempt_id=?", (attempt.id,)
        )
        self.refused()
        self.path.write_bytes(original)
        self.transaction(
            lambda: review_requests.retry_request(
                self.conn, self.request.id, 14.0
            )
        )
        self.path.unlink()
        self.refused()
        self.assertEqual(
            self.conn.execute(
                "SELECT state,reserved_at FROM review_call"
            ).fetchone()[:],
            ("unknown", 12.5),
        )
        self.assertEqual(
            review_requests.request_row(self.conn, self.request.id)[
                "cancel_code"
            ],
            "operator_retry",
        )

    def test_receipt_first_returns_completed_view_without_reads(self) -> None:
        self.receipt(self.request.id)
        self.conn.execute("UPDATE review_control SET admission=0,generation=2")
        self.path.unlink()
        before = self.state()
        with mock.patch.object(
            review_claims, "check_window", side_effect=AssertionError("read")
        ):
            for generation in (1, 2):
                result = self.claim(generation)
                assert isinstance(result, RequestView)
                self.assertEqual(result.state, "completed")
        self.assertEqual(self.state(), before)

    def test_guards_precede_source_classification(self) -> None:
        self.path.unlink()
        faults = (
            (
                "UPDATE review_control SET admission=0",
                "review_policy_required",
            ),
            (
                "UPDATE review_control SET policy_digest='changed'",
                "review_policy_required",
            ),
            ("UPDATE review_control SET policy_json='{}'", "validation"),
            (
                "UPDATE review_request SET state='cancelled'",
                "request_not_ready",
            ),
            (
                "UPDATE review_request SET state='waiting',retry_at=99.0",
                "request_not_ready",
            ),
        )
        for sql, code in faults:
            with self.subTest(code=code):
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.conn.execute(sql)
                    self.guard(code)
                finally:
                    self.conn.execute("ROLLBACK")
        for generation in (0, 2, True):
            self.transaction(
                lambda g=generation: self.guard("rejected_fenced", g)
            )
        with self.assertRaisesRegex(ValueError, "caller_transaction_required"):
            self.claim_current()

    def test_pointer_guards_precede_reads_and_never_touch_other_history(
        self,
    ) -> None:
        own = self.worker()
        self.fail_worker(own, "unavailable", 13.0)
        other = self.enqueue(self.window(2, 2))
        foreign = self.worker(other.id, 13.0)
        self.fail_worker(foreign, "config", 14.0)
        self.path.unlink()
        for pointer in ("A_missing", foreign.id, own.id):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE review_request SET current_attempt_id=? "
                    "WHERE id=?",
                    (pointer, self.request.id),
                )
                if pointer == own.id:
                    self.conn.execute(
                        "UPDATE review_attempt SET ended_at=NULL WHERE id=?",
                        (own.id,),
                    )
                self.guard("rejected_fenced")
            finally:
                self.conn.execute("ROLLBACK")

    def test_concurrency_and_attempt_limits_are_not_source_refusals(
        self,
    ) -> None:
        other = self.enqueue(self.window(2, 2))
        active = self.worker(other.id)
        self.transaction(lambda: self.guard("review_concurrency"))
        self.fail_worker(active, "config", 13.0)
        for now in (12.0, 14.0):
            attempt = self.worker(now=now)
            self.fail_worker(attempt, "unavailable", now)
        self.conn.execute(
            "UPDATE review_request SET state='pending' WHERE id=?",
            (self.request.id,),
        )
        self.transaction(lambda: self.guard("review_attempt_limit"))

    def test_only_exact_source_codes_are_classified(self) -> None:
        codes = (
            "source_unavailable",
            "original_unavailable",
            "ancestry_unavailable",
            "evidence_limit",
            "incomplete_window",
            "evidence_changed",
            "evidence_unavailable",
            "evidence_erased",
            "rejected_scope",
        )
        for code in codes:
            with (
                self.subTest(code=code),
                mock.patch.object(
                    review_claims, "check_window", side_effect=ValueError(code)
                ),
            ):
                self.refused()
            self.conn.execute("UPDATE review_request SET state='pending'")
        with self.assertRaises(ValueError) as invalid:
            Scope.model_validate_json("{}")
        errors = (
            ValueError("original_unavailable extra"),
            ValueError("review_policy_required"),
            invalid.exception,
            sqlite3.OperationalError("source_unavailable"),
            OSError("original_unavailable"),
            RuntimeError("evidence_changed"),
        )
        before = self.state()
        for error in errors:
            with (
                self.subTest(error=type(error)),
                mock.patch.object(
                    review_claims, "check_window", side_effect=error
                ),
            ):
                with self.assertRaises(type(error)) as caught:
                    self.claim()
                self.assertIs(caught.exception, error)
                self.assertEqual(self.state(), before)

    def test_refusal_and_postinsert_fault_remain_caller_atomic(self) -> None:
        original = self.path.read_bytes()
        self.path.unlink()
        before = self.state()
        with (
            store.writer_lock(self.home),
            mock.patch.object(
                store, "writer_lock", side_effect=AssertionError("nested")
            ),
        ):
            self.conn.execute("BEGIN IMMEDIATE")
            self.assertIsInstance(
                self.claim_current(),
                RequestView,
            )
            self.conn.execute("ROLLBACK")
        self.assertEqual(self.state(), before)
        self.path.write_bytes(original)
        with (
            mock.patch.object(
                review_claims,
                "attempt_view",
                side_effect=ValueError("evidence_changed"),
            ),
            self.assertRaisesRegex(ValueError, "evidence_changed"),
        ):
            self.claim()
        self.assertEqual(self.state(), before)

    def test_real_evidence_shares_one_aggregate_read_allowance(self) -> None:
        records = review_evidence.initial_evidence(
            self.home, self.window(), 4096
        ).excerpts
        for record in records:
            data = record.model_dump(mode="json")
            for key in ("scope", "selector", "original_locator"):
                data[f"{key}_json"] = canonical_json(getattr(record, key))
                del data[key]
            self.conn.execute(
                f"INSERT INTO review_evidence({','.join(data)}) "
                f"VALUES({','.join('?' for _ in data)})",
                tuple(data.values()),
            )
        exact = len(self.path.read_bytes()) + sum(
            len(line(self.records[r.line - 1])) for r in records
        )
        for cap in (exact - 1, exact):
            self.policy = self.policy.model_copy(
                update={"max_input_bytes": cap}
            )
            self.conn.execute(
                "UPDATE review_control SET policy_json=?,policy_digest=?",
                (canonical_json(self.policy), self.digest(self.policy)),
            )
            self.request = self.enqueue()
            self.conn.executemany(
                "INSERT INTO review_input VALUES(?,?)",
                [(self.request.id, r.id) for r in records],
            )
            with (
                mock.patch.object(
                    review_originals, "_jsonl", wraps=review_originals._jsonl
                ) as prefix,
                mock.patch.object(
                    review_evidence, "reread", wraps=review_evidence.reread
                ) as selected,
            ):
                result = self.claim()
            self.assertIsInstance(
                result, RequestView if cap < exact else AttemptView
            )
            budget = prefix.call_args.kwargs["budget"]
            self.assertEqual(selected.call_count, len(records))
            self.assertTrue(
                all(
                    call.kwargs["budget"] is budget
                    for call in selected.call_args_list
                )
            )
            if cap == exact:
                self.assertEqual(budget.bytes_left, 0)
