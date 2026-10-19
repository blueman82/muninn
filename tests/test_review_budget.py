"""Check durable shared account and per-policy launch reservations."""

from __future__ import annotations

import json
import os
from unittest import mock

from muninn import (
    review_budget,
    review_evidence,
    review_progress,
    review_requests,
    store,
)
from muninn.review_evidence_models import EvidenceLocator, EvidenceSelector
from muninn.review_models import ReviewContext
from tests.ingest_support import TID
from tests.test_classify import user_msg
from tests.test_review_requests import RequestCase


class ReviewBudgetTests(RequestCase):
    """Account conservatively across attempts, restarts and policy changes."""

    def context(self, round_index: int = 1) -> ReviewContext:
        """Claim a request and construct its explicit runtime context."""
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
            round_index=round_index,
            deadline=72.0,
        )

    def cost_policy(self, name: str, cap: str, charge: str) -> None:
        """Install an explicit synthetic charge bound and policy identity."""
        self.policy = self.policy.model_copy(
            update={
                "policy_id": name,
                "budget": self.policy.budget.model_copy(
                    update={
                        "max_cost_decimal": cap,
                        "currency": "EUR",
                        "conservative_call_cost_decimal": charge,
                        "max_model_calls": 10,
                    }
                ),
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )

    def test_shared_cost_caps_keep_unknown_charges_across_policies(
        self,
    ) -> None:
        for name in ("first", "second"):
            self.cost_policy(name, "2.00", "0.75")
            context = self.context()
            self.conn.execute("BEGIN IMMEDIATE")
            call = review_budget.reserve_call(self.conn, context, 13.0)
            review_requests.cancel_request(self.conn, context.request_id, 14.0)
            self.conn.execute("COMMIT")
            self.assertEqual(call.cost_reservation_decimal, "0.75")
        self.cost_policy("third", "20.00", "0.75")
        context = self.context()
        with self.assertRaisesRegex(ValueError, "budget_cost_limit"):
            self.transaction(
                lambda: review_budget.reserve_call(self.conn, context, 15.0)
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*),min(reserved_at) FROM review_call"
            ).fetchone()[:],
            (2, 13.0),
        )

    def test_reservation_precedes_launch(self) -> None:
        context = self.context()
        call = self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 13.0)
        )
        self.assertEqual(call.state, "reserved")
        self.assertIsNone(call.launched_at)
        self.assertIsNone(call.process_identity)
        self.assertIsNone(call.usage)
        duplicate = self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 14.0)
        )
        self.assertEqual(call, duplicate)
        with store.connect_ro(store.db_path(self.home)) as reopened:
            self.addCleanup(reopened.close)
            self.assertEqual(
                reopened.execute(
                    "SELECT count(*) FROM review_call"
                ).fetchone()[0],
                1,
            )

    def test_crash_unknown_counts_conservatively(self) -> None:
        self.cost_policy("cost", "2.00", "0.75")
        context = self.context()
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 13.0)
        )
        self.conn.close()
        self.conn = store.connect_rw(store.db_path(self.home), fullfsync=False)
        self.addCleanup(self.conn.close)
        self.conn.execute("UPDATE review_control SET generation=2")
        self.transaction(
            lambda: review_requests.recover_attempts(self.conn, 2, 14.0)
        )
        row = self.conn.execute(
            "SELECT state,cost_reservation_decimal,reserved_at "
            "FROM review_call"
        ).fetchone()
        self.assertEqual(row[:], ("unknown", "0.75", 13.0))
        self.assertIsNone(
            review_budget.aggregate_usage(
                self.conn, self.policy.budget.account_id
            ).cost_decimal
        )

    def test_shared_account_cap_cannot_reset(self) -> None:
        context = self.context()
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 13.0)
        )
        self.conn.execute("UPDATE review_call SET state='completed'")
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, context.model_copy(update={"round_index": 2}), 14.0
            )
        )
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, context.request_id, 15.0
            )
        )
        self.policy = self.policy.model_copy(
            update={
                "policy_id": "changed",
                "budget": self.policy.budget.model_copy(
                    update={"max_model_calls": 100}
                ),
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )
        changed = self.context()
        with self.assertRaisesRegex(ValueError, "budget"):
            self.transaction(
                lambda: review_budget.reserve_call(self.conn, changed, 16.0)
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*),min(reserved_at) FROM review_call"
            ).fetchone()[:],
            (2, 13.0),
        )

    def test_missing_usage_stays_unknown(self) -> None:
        context = self.context()

        def reserve(number: int) -> None:
            self.transaction(
                lambda: review_budget.reserve_call(
                    self.conn,
                    context.model_copy(update={"round_index": number}),
                    12.0 + number,
                )
            )
            self.conn.execute("UPDATE review_call SET state='completed'")

        for number in (1, 2):
            reserve(number)
        self.conn.execute(
            "UPDATE review_call SET state='completed',usage_json=? "
            "WHERE round_index=1",
            (
                '{"input_tokens":10,"output_tokens":3,"cached_tokens":1,'
                '"cost_decimal":"0.1","currency":"EUR"}',
            ),
        )
        self.conn.execute(
            "UPDATE review_call SET state='completed',usage_json=? "
            "WHERE round_index=2",
            (
                '{"input_tokens":20,"output_tokens":null,"cached_tokens":2,'
                '"cost_decimal":null,"currency":"EUR"}',
            ),
        )
        usage = review_budget.aggregate_usage(
            self.conn, self.policy.budget.account_id
        )
        self.assertEqual(usage.input_tokens, 30)
        self.assertEqual(usage.cached_tokens, 3)
        self.assertIsNone(usage.output_tokens)
        self.assertIsNone(usage.cost_decimal)

    def test_wall_budget_keeps_earliest_reservation(self) -> None:
        context = self.context()
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 13.0)
        )
        with self.assertRaisesRegex(ValueError, "budget"):
            self.transaction(
                lambda: review_budget.reserve_call(
                    self.conn,
                    context.model_copy(update={"round_index": 2}),
                    73.0,
                )
            )

    def test_cost_cap_without_approved_charge_fails_closed(self) -> None:
        self.policy = self.policy.model_copy(
            update={
                "budget": self.policy.budget.model_copy(
                    update={"max_cost_decimal": "2.00", "currency": "EUR"}
                )
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )
        context = self.context()
        with self.assertRaisesRegex(ValueError, "budget_charge_unavailable"):
            self.transaction(
                lambda: review_budget.reserve_call(self.conn, context, 13.0)
            )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            0,
        )

    def test_fenced_context_cannot_reserve(self) -> None:
        context = self.context()
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, context.request_id, 13.0
            )
        )
        with self.assertRaises(ValueError):
            self.transaction(
                lambda: review_budget.reserve_call(self.conn, context, 14.0)
            )

    def test_unfinished_round_blocks_the_next_reservation(self) -> None:
        context = self.context()
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 13.0)
        )
        with self.assertRaisesRegex(ValueError, "round"):
            self.transaction(
                lambda: review_budget.reserve_call(
                    self.conn,
                    context.model_copy(update={"round_index": 2}),
                    14.0,
                )
            )


class ReviewAdmissionEdgeTests(RequestCase):
    """Exercise disabled admission and stale policy control boundaries."""

    def test_disabled_admission_cannot_enqueue(self) -> None:
        self.conn.execute("UPDATE review_control SET admission=0")
        with self.assertRaisesRegex(ValueError, "review_policy_required"):
            self.enqueue()

    def test_control_snapshot_must_match_its_digest(self) -> None:
        request = self.enqueue()
        self.conn.execute("UPDATE review_control SET policy_json=NULL")
        with self.assertRaisesRegex(ValueError, "review_policy_required"):
            self.transaction(
                lambda: review_requests.claim_attempt(
                    self.conn, request.id, 1, 12.0
                )
            )

    def test_cancelled_gap_does_not_suppress_later_eligible_record(
        self,
    ) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        additions = [user_msg(4, "old"), {"type": "noop"}, user_msg(6, "new")]
        self.records.extend(additions)
        self.append(self.path, additions[:2])
        self.run_ingest()
        cancelled = self.enqueue(self.window(4, 5))
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, cancelled.id, 12.0
            )
        )
        self.append(self.path, additions[2:])
        self.run_ingest()
        requests = self.transaction(
            lambda: review_requests.reconcile_sources(
                self.conn, self.policy, 13.0
            )
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT from_line,through_line FROM review_request WHERE id=?",
                (requests[0],),
            ).fetchone()[:],
            (6, 6),
        )

    def test_retry_revalidates_selected_original_after_indexing(self) -> None:
        roots = {key: str(value) for key, value in self.roots.items()}
        with mock.patch.dict(os.environ, {"MUNINN_ROOTS": json.dumps(roots)}):
            record = review_evidence.select_evidence(
                self.home,
                EvidenceLocator(
                    source=self.window().source,
                    line=2,
                    part=1,
                    line_sha256=self.window(2, 2).boundary_hash,
                ),
                EvidenceSelector(
                    json_pointer="/payload/content/0/text",
                    start=None,
                    end=None,
                ),
            )
            data = record.model_dump(mode="json")
            for field in ("original_locator", "selector", "scope"):
                data[f"{field}_json"] = json.dumps(data.pop(field))
            self.conn.execute(
                f"INSERT INTO review_evidence({','.join(data)}) "
                f"VALUES({','.join('?' for _ in data)})",
                tuple(data.values()),
            )
            request = self.enqueue()
            self.conn.execute(
                "INSERT INTO review_input VALUES(?,?)", (request.id, record.id)
            )
            self.transaction(
                lambda: review_requests.cancel_request(
                    self.conn, request.id, 13.0
                )
            )
            self.path.write_bytes(
                self.path.read_bytes().replace(b"q one", b"q two")
            )
            with self.assertRaisesRegex(ValueError, "evidence_changed"):
                self.transaction(
                    lambda: review_requests.retry_request(
                        self.conn, request.id, 14.0
                    )
                )

    def test_missing_ancestor_blocks_enqueue(self) -> None:
        self.conn.execute(
            "UPDATE source SET forked_from_id='missing' WHERE thread_id=?",
            (TID,),
        )
        with self.assertRaisesRegex(ValueError, "ancestry_unavailable"):
            self.enqueue()

    def test_duplicate_pending_request_does_not_bypass_erasure(self) -> None:
        self.enqueue()
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',?,2,?,14.0)",
            (TID, self.window(2, 2).boundary_hash),
        )
        with self.assertRaisesRegex(ValueError, "evidence_erased"):
            self.enqueue()

    def test_old_generation_failure_cannot_change_current_request(
        self,
    ) -> None:
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.conn.execute("UPDATE review_control SET generation=2")
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.transaction(
                lambda: review_requests.fail_attempt(
                    self.conn, attempt.id, "unavailable", 13.0
                )
            )

    def test_boundary_prefix_exceeding_policy_bytes_is_not_admitted(
        self,
    ) -> None:
        self.policy = self.policy.model_copy(update={"max_input_bytes": 1000})
        with self.assertRaisesRegex(ValueError, "evidence_limit"):
            self.enqueue()
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_source").fetchone()[
                0
            ],
            0,
        )
