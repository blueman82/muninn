"""Keep completed review history outside unfinished host containment."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from unittest import mock

from install.context import StepFailedError
from install.host_renewal_preview import preview, scan_state
from muninn import review_commit, review_evidence, review_requests, store
from muninn.knowledge_models import ReviewResult
from muninn.review_commit import CommitContext
from muninn.review_models import Receipt
from muninn.review_progress import digest
from muninn.review_snapshot import snapshot
from tests.host_renewal_inventory import seed_indexed
from tests.host_renewal_support import states
from tests.test_host_renewal_apply import ApplyFixture


class HostRenewalHistoryTests(ApplyFixture):
    """Exercise retained pointers written by the actual completion path."""

    def complete_history(self) -> Receipt:
        """Admit, claim and commit a real review of disposable originals."""
        source = seed_indexed(self)
        evidence = review_evidence.initial_evidence(
            self.ctx.data, source.window(), self.old.max_input_bytes
        ).excerpts
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE review_control SET generation=1")
            generation = conn.execute(
                "SELECT generation FROM review_control"
            ).fetchone()[0]
            request = review_requests.enqueue_review(
                conn, source.window(), source.scope, self.old, "explicit", 10.0
            )
            for record in evidence:
                data = record.model_dump(mode="json")
                for key in ("scope", "selector", "original_locator"):
                    data[key + "_json"] = json.dumps(data.pop(key))
                names, marks = ",".join(data), ",".join("?" for _ in data)
                conn.execute(
                    f"INSERT INTO review_evidence({names}) VALUES({marks})",
                    tuple(data.values()),
                )
                conn.execute(
                    "INSERT INTO review_input VALUES(?,?)",
                    (request.id, record.id),
                )
            attempt = review_requests.claim_attempt(
                conn, request.id, generation, 11.0
            )
            conn.commit()
            context = CommitContext(
                home=self.ctx.data,
                request_id=request.id,
                attempt_id=attempt.id,
                generation=generation,
                evidence_ids=tuple(record.id for record in evidence),
                now=12.0,
                knowledge_digest=snapshot(conn, source.scope),
            )
            return review_commit.commit_review(
                conn,
                context,
                ReviewResult.model_validate_json(
                    '{"schema_version":1,"changes":[],"unresolved":[], '
                    '"no_change_reason":"No change."}'
                ),
            )

    def test_normal_completed_review_retains_pointer_without_job(self) -> None:
        receipt = self.complete_history()
        with closing(store.connect_ro(self.path)) as conn:
            row = conn.execute(
                "SELECT state,current_attempt_id FROM review_request"
            ).fetchone()
            self.assertEqual(tuple(row), ("completed", receipt.attempt_id))
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_receipt").fetchone()[
                    0
                ],
                1,
            )
        before = states(self.home)
        result = self.scan()
        self.assertEqual(result.request_ids, (receipt.request_id,))
        self.assertEqual(result.attempt_ids, (receipt.attempt_id,))
        self.assertEqual(result.containment.jobs, ())
        plan = preview(self.ctx, self.repo, self.sha)
        self.assertEqual(plan.containment.jobs, ())
        self.assertEqual(states(self.home), before)

    def history_rows(
        self, receipt: Receipt
    ) -> dict[str, list[tuple[object, ...]]]:
        """Read immutable completion rows independently of scan hashes."""
        selectors = {
            "review_request": ("id=?", (receipt.request_id,)),
            "review_attempt": ("id=?", (receipt.attempt_id,)),
            "review_call": ("attempt_id=?", (receipt.attempt_id,)),
            "review_receipt": ("request_id=?", (receipt.request_id,)),
            "review_input": ("request_id=?", (receipt.request_id,)),
            "review_evidence": ("1", ()),
            "review_source": ("1", ()),
            "learning_setup": ("1", ()),
        }
        with closing(store.connect_ro(self.path)) as conn:
            return {
                table: [
                    tuple(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} WHERE {where} ORDER BY 1",
                        params,
                    )
                ]
                for table, (where, params) in selectors.items()
            }

    def test_completed_history_survives_actual_no_call_containment(
        self,
    ) -> None:
        receipt = self.complete_history()
        self.insert(
            "review_call",
            {
                "attempt_id": receipt.attempt_id,
                "round_index": 1,
                "budget_account_id": "synthetic-account",
                "policy_digest": digest(self.old),
                "state": "completed",
                "reserved_at": 11.0,
                "ended_at": 12.0,
                "requested_json": "{}",
                "observed_json": "{}",
                "cost_reservation_decimal": "0.75",
                "currency": "USD",
            },
        )
        before = self.history_rows(receipt)
        active = self.request(number=2)
        active_attempt = self.attempt(active, number=2)
        cancelled = self.request(number=3)
        cancelled_attempt = self.attempt(cancelled, number=3)
        self.sql(
            "UPDATE review_request SET state='cancelled' WHERE id=?",
            (cancelled,),
        )
        plan = preview(self.ctx, self.repo, self.sha)
        self.assertEqual(
            {job.attempt.id for job in plan.containment.jobs},
            {active_attempt, cancelled_attempt},
        )
        self.assertIn(receipt.request_id, plan.request_ids)
        self.assertIn(receipt.attempt_id, plan.attempt_ids)
        with mock.patch("install.cutover.time.time", self.ctx.now):
            self.run_renewal(plan)
        self.assertEqual(self.history_rows(receipt), before)
        after = self.scan(plan.containment)
        self.assertEqual(len(after.containment.jobs), 2)
        self.assertTrue(
            all(
                job.attempt.outcome == "abandoned"
                for job in after.containment.jobs
            )
        )

    def test_completed_pointer_requires_own_existing_ended_attempt(
        self,
    ) -> None:
        self.seed_inventory()
        other = self.request("completed", number=2)
        other_attempt = self.attempt(other, number=2)
        self.sql(
            "UPDATE review_attempt SET ended_at=3.0,outcome='completed' "
            "WHERE id=?",
            (other_attempt,),
        )
        self.sql(
            "UPDATE review_request SET state='completed',"
            "current_attempt_id=NULL "
            "WHERE id=?",
            (other,),
        )
        request, attempt = "R_" + "1" * 32, "A_" + "1" * 32
        for pointer in ("malformed", "A_" + "f" * 32, other_attempt):
            with self.subTest(pointer=pointer):
                self.sql(
                    "UPDATE review_request SET current_attempt_id=? "
                    "WHERE id=?",
                    (pointer, request),
                )
                with self.assertRaisesRegex(
                    StepFailedError, "host_renewal_state_invalid"
                ):
                    self.scan()
        self.sql(
            "UPDATE review_request SET current_attempt_id=? WHERE id=?",
            (attempt, request),
        )
        self.sql(
            "UPDATE review_attempt SET ended_at=NULL WHERE id=?", (attempt,)
        )
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_state_invalid"
        ):
            self.scan()

    def test_duplicate_completed_pointers_refuse_in_either_order(self) -> None:
        first, second = self.request(), self.request(number=2)
        attempts = self.attempt(first), self.attempt(second, number=2)
        self.sql("UPDATE review_attempt SET ended_at=3.0,outcome='completed'")
        for pointer in attempts:
            with self.subTest(pointer=pointer):
                self.sql(
                    "UPDATE review_request SET state='completed',"
                    "current_attempt_id=?",
                    (pointer,),
                )
                with self.assertRaisesRegex(
                    StepFailedError, "host_renewal_state_invalid"
                ):
                    self.scan()

    def test_completed_attempt_still_requires_strict_view(self) -> None:
        self.seed_inventory()
        self.sql("UPDATE review_attempt SET generation=0")
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_state_invalid"
        ):
            self.scan()

    def test_other_five_states_keep_ended_pointer_refusal(self) -> None:
        request = self.request()
        self.attempt(request)
        self.sql("UPDATE review_attempt SET ended_at=3.0,outcome='completed'")
        for state in ("pending", "running", "waiting", "failed", "cancelled"):
            with self.subTest(state=state):
                self.sql("UPDATE review_request SET state=?", (state,))
                with self.assertRaisesRegex(
                    StepFailedError, "host_renewal_state_invalid"
                ):
                    self.scan()

    def test_unresolved_calls_refuse_across_all_request_states(self) -> None:
        self.seed_inventory()
        for state in (
            "pending",
            "running",
            "waiting",
            "completed",
            "failed",
            "cancelled",
        ):
            pointer = "A_" + "1" * 32 if state == "completed" else None
            self.sql(
                "UPDATE review_request SET state=?,current_attempt_id=?",
                (state, pointer),
            )
            self.sql("UPDATE review_call SET state='completed'")
            self.assertEqual(self.scan().containment.jobs, ())
            for call_state in ("reserved", "launched", "unknown"):
                with self.subTest(state=state, call_state=call_state):
                    self.sql("UPDATE review_call SET state=?", (call_state,))
                    with self.assertRaisesRegex(
                        StepFailedError, "host_renewal_containment_required"
                    ):
                        self.scan()

    def test_supplied_job_cannot_disappear_when_request_completes(
        self,
    ) -> None:
        request = self.request()
        self.attempt(request)
        before = self.scan()
        self.assertEqual(len(before.containment.jobs), 1)
        self.sql("UPDATE review_request SET state='completed'")
        self.sql("UPDATE review_attempt SET ended_at=3.0,outcome='completed'")
        self.assertEqual(self.scan().containment.jobs, ())
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_state_changed"
        ):
            self.scan(before.containment)

    def test_completed_history_is_fully_bound_in_supplied_scan(self) -> None:
        self.seed_inventory()
        request = self.request(number=2)
        self.attempt(request, number=2)
        before = self.scan()
        mutations = (
            ("review_request", "state", "failed"),
            ("review_request", "retry_at", 7.0),
            ("review_request", "current_attempt_id", None),
            ("review_attempt", "ended_at", 4.0),
            ("review_attempt", "progress_at", 4.0),
            ("review_attempt", "outcome", "no_change"),
            ("review_call", "cost_reservation_decimal", "0.75"),
            ("review_receipt", "no_change_reason", "Changed reason"),
        )
        with closing(sqlite3.connect(self.path)) as conn:
            for table, column, value in mutations:
                with self.subTest(table=table, column=column):
                    conn.execute("SAVEPOINT history")
                    try:
                        where = (
                            "WHERE id=?"
                            if table in {"review_request", "review_attempt"}
                            else ""
                        )
                        params = (
                            (
                                value,
                                ("R_" if table == "review_request" else "A_")
                                + "1" * 32,
                            )
                            if where
                            else (value,)
                        )
                        conn.execute(
                            f"UPDATE {table} SET {column}=? {where}", params
                        )
                        after = scan_state(
                            conn,
                            self.path,
                            candidate_digest=digest(self.candidate),
                            containment=before.containment,
                        )
                        self.assertNotEqual(
                            after.state.work_digest, before.state.work_digest
                        )
                        self.assertNotEqual(
                            after.containment.invariant_digest,
                            before.containment.invariant_digest,
                        )
                        self.assertEqual(
                            after.containment.jobs, before.containment.jobs
                        )
                    finally:
                        conn.execute("ROLLBACK TO history")
                        conn.execute("RELEASE history")
        self.assertEqual(self.scan(), before)
