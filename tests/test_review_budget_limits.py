"""Exercise explicit account limits, currency and restart accounting."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any
from unittest import mock

from muninn import review_budget, review_progress, review_requests, store
from muninn.review_json import canonical_json
from muninn.review_models import RequestView, ReviewContext
from tests.ingest_support import rollout
from tests.test_classify import codex_meta, reply, user_msg
from tests.test_review_requests import RequestCase


class ReviewBudgetLimitTests(RequestCase):
    """Keep account charges and clocks durable through policy changes."""

    def install(self, name: str, **budget: object) -> None:
        """Install one explicit synthetic policy variant for a bounded case."""
        self.policy = self.policy.model_copy(
            update={
                "policy_id": name,
                "budget": self.policy.budget.model_copy(update=budget),
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def context(self) -> ReviewContext:
        """Claim a synthetic request and bind the exact immutable policy."""
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        return ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=request.scope,
            policy=self.policy,
            admitted_windows=(request.window,),
            evidence_ids=(),
            round_index=1,
            deadline=72.0,
        )

    def reserve(self, context: ReviewContext, now: float) -> None:
        """Reserve and commit one call without launching any process."""
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, now)
        )

    def cancel(self, context: ReviewContext) -> None:
        """Cancel an attempt while retaining its unknown launch status."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, context.request_id, 14.0
            )
        )

    def test_currency_change_fails_closed(self) -> None:
        self.install("first", currency="EUR")
        first = self.context()
        self.reserve(first, 13.0)
        self.cancel(first)
        self.install("second", currency="USD")
        with self.assertRaisesRegex(ValueError, "budget_currency_mismatch"):
            self.reserve(self.context(), 15.0)

    def test_stricter_later_policy_conservatively_caps_shared_totals(
        self,
    ) -> None:
        self.install(
            "first",
            max_cost_decimal="10.00",
            currency="EUR",
            conservative_call_cost_decimal="1.00",
        )
        first = self.context()
        self.reserve(first, 13.0)
        self.cancel(first)
        self.install("second", max_cost_decimal="1.50")
        with self.assertRaisesRegex(ValueError, "budget_cost_limit"):
            self.reserve(self.context(), 15.0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_explicit_retry_cannot_reset_wall_clock(self) -> None:
        first = self.context()
        self.reserve(first, 13.0)
        self.cancel(first)
        self.transaction(
            lambda: review_requests.retry_request(
                self.conn, first.request_id, 80.0
            )
        )
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, first.request_id, 1, 81.0
            )
        )
        with self.assertRaisesRegex(ValueError, "budget_wall_limit"):
            self.reserve(
                first.model_copy(
                    update={"attempt_id": attempt.id, "deadline": 141.0}
                ),
                81.0,
            )

    def test_restart_cannot_reset_wall_clock_or_release_call(self) -> None:
        first = self.context()
        self.reserve(first, 13.0)
        self.conn.close()
        self.conn = store.connect_rw(store.db_path(self.home), fullfsync=False)
        self.addCleanup(self.conn.close)
        self.conn.execute("UPDATE review_control SET generation=2")
        self.transaction(
            lambda: review_requests.recover_attempts(self.conn, 2, 75.0)
        )
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, first.request_id, 2, 76.0
            )
        )
        with self.assertRaisesRegex(ValueError, "budget_wall_limit"):
            self.reserve(
                first.model_copy(
                    update={
                        "attempt_id": attempt.id,
                        "generation": 2,
                        "deadline": 136.0,
                    }
                ),
                76.0,
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT state,reserved_at FROM review_call"
            ).fetchone()[:],
            ("unknown", 13.0),
        )

    def test_unknown_round_keeps_currency_unknown(self) -> None:
        first = self.context()
        self.reserve(first, 13.0)
        self.conn.execute(
            "UPDATE review_call SET state='completed',usage_json=?",
            (
                '{"input_tokens":10,"output_tokens":3,"cached_tokens":1,'
                '"cost_decimal":"0.1","currency":"EUR"}',
            ),
        )
        self.reserve(first.model_copy(update={"round_index": 2}), 14.0)
        usage = review_budget.aggregate_usage(
            self.conn, self.policy.budget.account_id
        )
        self.assertIsNone(usage.input_tokens)
        self.assertIsNone(usage.currency)
        self.assertIsNone(usage.cost_decimal)

    def test_actual_completed_charge_replaces_conservative_reservation(
        self,
    ) -> None:
        self.install(
            "cost",
            max_cost_decimal="1.00",
            currency="EUR",
            conservative_call_cost_decimal="0.75",
        )
        context = self.context()
        self.reserve(context, 13.0)
        self.conn.execute(
            "UPDATE review_call SET state='completed',usage_json=?",
            (
                '{"input_tokens":null,"output_tokens":null,"cached_tokens":null,'
                '"cost_decimal":"0.10","currency":"EUR"}',
            ),
        )
        self.reserve(context.model_copy(update={"round_index": 2}), 14.0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            2,
        )

    def test_reservation_uses_caller_transaction_and_rollback(self) -> None:
        context = self.context()
        with store.writer_lock(self.home, wait_s=0.0):
            self.conn.execute("BEGIN IMMEDIATE")
            review_budget.reserve_call(self.conn, context, 13.0)
            self.assertTrue(self.conn.in_transaction)
            self.conn.execute("ROLLBACK")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            0,
        )

    def test_unknown_cost_cannot_be_replaced_by_later_report(self) -> None:
        self.install(
            "first",
            max_cost_decimal="1.00",
            currency="EUR",
            conservative_call_cost_decimal="0.75",
        )
        first = self.context()
        self.reserve(first, 13.0)
        self.cancel(first)
        self.conn.execute(
            "UPDATE review_call SET usage_json=?",
            (
                '{"input_tokens":0,"output_tokens":0,"cached_tokens":0,'
                '"cost_decimal":"0.10","currency":"EUR"}',
            ),
        )
        self.install("second")
        with self.assertRaisesRegex(ValueError, "budget_cost_limit"):
            self.reserve(self.context(), 15.0)


class ReviewProgressEdgeTests(RequestCase):
    """Separate explicit bounds, activation and successful coverage."""

    def test_explicit_windows_keep_baseline_and_uncovered_gaps(self) -> None:
        self.conn.execute("UPDATE source SET first_seen=0.0")
        self.enqueue()
        self.assertEqual(
            self.conn.execute(
                "SELECT activation_line,eligible_line,completed_line "
                "FROM review_source"
            ).fetchone()[:],
            (3, 3, 3),
        )
        additions = [user_msg(4, "four"), user_msg(5, "five")]
        self.records.extend(additions)
        self.append(self.path, additions)
        self.run_ingest()
        later = self.enqueue(self.window(5, 5))
        self.receipt(later.id)
        self.transaction(
            lambda: review_progress.advance_completed(
                self.conn, self.window().source, 15.0
            )
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT activation_line,eligible_line,completed_line "
                "FROM review_source"
            ).fetchone()[:],
            (3, 5, 3),
        )
        gap = self.enqueue(self.window(4, 4))
        self.receipt(gap.id)
        self.transaction(
            lambda: review_progress.advance_completed(
                self.conn, self.window().source, 16.0
            )
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT activation_line,eligible_line,completed_line,"
                "boundary_hash "
                "FROM review_source"
            ).fetchone()[:],
            (3, 5, 5, self.window(5, 5).boundary_hash),
        )

    def test_replaced_index_cannot_silently_reuse_eligible_boundary(
        self,
    ) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        self.records[-1] = reply(2, "changed answer")
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        before = tuple(
            self.conn.execute("SELECT * FROM review_source").fetchone()
        )
        self.assertEqual(
            self.transaction(
                lambda: review_requests.reconcile_sources(
                    self.conn, self.policy, 12.0
                )
            ),
            (),
        )
        self.assertEqual(
            tuple(self.conn.execute("SELECT * FROM review_source").fetchone()),
            before,
        )
        record = json.loads((self.home / "calls.jsonl").read_text())
        self.assertEqual(record["error_code"], "rejected_evidence")

    def test_one_refused_source_does_not_starve_other_sources(self) -> None:
        self.policy = self.policy.model_copy(update={"max_input_bytes": 8192})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.append(self.path, [{"type": "noop"}] * 400)
        self.run_ingest()
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        progress = self.conn.execute("SELECT * FROM review_source").fetchone()
        archived = progress[:]
        fresh = self.write(
            rollout("fresh"),
            [codex_meta("user", "fresh"), user_msg(1, "fresh")],
        )
        self.run_ingest()
        log = self.home / "calls.jsonl"
        for number in range(3):
            with self.subTest(number=number):
                if number:
                    self.append(self.path, [user_msg(500 + number, "refused")])
                    self.append(fresh, [user_msg(number + 1, "independent")])
                    self.run_ingest()
                previous_log = log.read_bytes() if log.exists() else b""
                with mock.patch.dict(
                    os.environ,
                    {"MUNINN_NO_CALLLOG": "1" if number == 2 else "0"},
                ):
                    ids = self.transaction(
                        lambda n=number: review_requests.reconcile_sources(
                            self.conn, self.policy, 12.0 + n
                        )
                    )
                self.assertEqual(len(ids), 1)
                self.assertEqual(
                    tuple(
                        self.conn.execute(
                            "SELECT * FROM review_source WHERE thread_id!=?",
                            ("fresh",),
                        ).fetchone()
                    ),
                    archived,
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM review_request "
                        "WHERE thread_id='fresh'"
                    ).fetchone()[0],
                    number + 1,
                )
                if number == 1:
                    record = json.loads(log.read_text())
                    self.assertIsInstance(record.pop("at"), float)
                    self.assertEqual(
                        record,
                        {
                            "event": "review_enqueued",
                            "accepted": False,
                            "error_code": "unavailable",
                            "recovery_gaps": 1,
                        },
                    )
                if number == 2:
                    self.assertEqual(log.read_bytes(), previous_log)
        self.assertEqual(
            self.conn.execute(
                "SELECT completed_line FROM review_source "
                "WHERE thread_id='fresh'"
            ).fetchone()[0],
            0,
        )

    def test_source_savepoint_preserves_caller_transaction(self) -> None:
        original = review_requests.enqueue_review
        count = 0

        def refusing(*args: Any, **kwargs: Any) -> RequestView:
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError("evidence_changed")
            return original(*args, **kwargs)

        self.conn.execute("BEGIN IMMEDIATE")
        with mock.patch.object(
            review_requests, "enqueue_review", side_effect=refusing
        ):
            self.assertEqual(
                review_requests.reconcile_sources(
                    self.conn, self.policy, 12.0
                ),
                (),
            )
        self.assertTrue(self.conn.in_transaction)
        counts = self.conn.execute(
            "SELECT count(*) FROM review_request UNION ALL "
            "SELECT count(*) FROM review_source"
        ).fetchall()
        self.assertEqual([row[0] for row in counts], [0, 0])
        self.conn.execute("ROLLBACK")
        for failure in [
            sqlite3.OperationalError("synthetic"),
            ValueError("unknown_failure"),
        ]:
            with (
                mock.patch.object(
                    review_requests, "enqueue_review", side_effect=failure
                ),
                self.assertRaises(type(failure)),
            ):
                self.transaction(
                    lambda: review_requests.reconcile_sources(
                        self.conn, self.policy, 13.0
                    )
                )
