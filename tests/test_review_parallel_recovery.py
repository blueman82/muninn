"""Exercise real parallel wrappers and bounded request retries."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from muninn import (
    platform_lock,
    review_requests,
    review_supervisor,
    review_supervisor_result,
)
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext
from muninn.review_process import ProcessOutcome
from muninn.review_round_state import Round
from tests.test_review_requests import RequestCase
from tests.test_review_supervisor import SupervisorCase


class ParallelReviewTests(SupervisorCase):
    """Use fake provider processes with actual main-thread coordination."""

    def test_manual_request_precedes_newer_catchup(self) -> None:
        self.conn.execute(
            "UPDATE review_request SET trigger='history',created_at=1.0"
        )
        manual = self.enqueue(self.window(1, 2))
        recovered = self.transaction(
            lambda: review_requests.enqueue_review(
                self.conn,
                self.window(3, 3),
                self.scope,
                self.policy,
                "reconcile",
                12.0,
            )
        )
        with mock.patch.object(self.supervisor, "_spawn"):
            self.supervisor.tick(13.0)
        selected = next(iter(self.supervisor._rounds.values()))
        self.assertEqual(selected.context.request_id, manual.id)
        self.assertNotEqual(selected.context.request_id, recovered.id)

    def test_four_workers_claim_live_work_before_older_history(self) -> None:
        self.supervisor.stop(12.0)
        self.policy = self.policy.model_copy(
            update={
                "concurrency": 4,
                "budget": self.policy.budget.model_copy(
                    update={"max_model_calls": 10}
                ),
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.supervisor = review_supervisor.Supervisor(self.home, self.policy)
        self.supervisor.start(12.0)
        self.addCleanup(self.supervisor.stop, 20.0)
        history = []
        for first, last in ((1, 2), (1, 3), (2, 2), (2, 3)):
            history.append(
                self.transaction(
                    lambda lo=first, hi=last: review_requests.enqueue_review(
                        self.conn,
                        self.window(lo, hi),
                        self.scope,
                        self.policy,
                        "history",
                        13.0,
                    )
                )
            )
        live = self.transaction(
            lambda: review_requests.enqueue_review(
                self.conn,
                self.window(3, 3),
                self.scope,
                self.policy,
                "hook",
                14.0,
            )
        )
        self.supervisor.tick(15.0)
        self.assertEqual(len(self.supervisor._rounds), 4)
        self.assertIn(
            live.id,
            {r.context.request_id for r in self.supervisor._rounds.values()},
        )
        began = time.monotonic()
        until = began + 12.0
        while time.monotonic() < until:
            self.supervisor.tick(16.0 + time.monotonic() - began)
            if (
                self.conn.execute(
                    "SELECT count(*) FROM review_receipt"
                ).fetchone()[0]
                == 5
            ):
                break
            time.sleep(0.01)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_receipt"
            ).fetchone()[0],
            5,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_call WHERE state!='completed'"
            ).fetchone()[0],
            0,
        )

    def test_parallel_retry_reaches_five_receipts(self) -> None:
        real_execute = review_supervisor.execute_result
        real_refresh = review_supervisor_result.review_prompt.refresh
        selected: list[tuple[str, str, float]] = []
        raised: list[str] = []
        self.addCleanup(lambda: self.supervisor.stop(20.0))

        def execute(
            home: Path,
            pending: Round,
            outcome: ProcessOutcome,
            now: float,
            fail: Callable[[ReviewContext, str, float, str], None],
        ) -> bool:
            if selected:
                return real_execute(home, pending, outcome, now, fail)
            context = pending.context
            self.assertEqual(home, self.home)
            self.assertEqual(outcome.attempt_id, context.attempt_id)
            self.assertEqual(outcome.round_index, context.round_index)
            self.assertEqual(
                (outcome.error_code, outcome.exit_code), (None, 0)
            )
            assert pending.handle is not None
            self.assertIsNotNone(pending.handle.wrapper.returncode)
            self.assertFalse(self.conn.in_transaction)
            states = self.conn.execute(
                "SELECT c.state,r.state FROM review_call c "
                "JOIN review_attempt a ON a.id=c.attempt_id "
                "JOIN review_request r ON r.id=a.request_id "
                "JOIN review_control k ON k.policy_digest=r.policy_digest "
                "WHERE c.attempt_id=? AND c.round_index=? AND r.id=?",
                (context.attempt_id, context.round_index, context.request_id),
            ).fetchone()
            self.assertEqual(tuple(states or ()), ("completed", "running"))
            selected.append((context.request_id, context.attempt_id, now))

            def refresh(path: Path, bound: ReviewContext) -> ReviewContext:
                if path == home and bound is context:
                    self.assertFalse(raised)
                    raised.append(context.attempt_id)
                    raise ValueError("rejected_revision")
                return real_refresh(path, bound)

            with mock.patch.object(
                review_supervisor_result.review_prompt,
                "refresh",
                side_effect=refresh,
            ):
                return real_execute(home, pending, outcome, now, fail)

        with mock.patch.object(
            review_supervisor, "execute_result", side_effect=execute
        ):
            self.test_four_workers_claim_live_work_before_older_history()
        self.assertEqual(len(selected), 1)
        request_id, attempt_id, failed_at = selected[0]
        self.assertEqual(raised, [attempt_id])
        attempts = self.conn.execute(
            "SELECT id,error_code,ended_at,started_at FROM review_attempt "
            "WHERE request_id=? ORDER BY started_at,id",
            (request_id,),
        ).fetchall()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            tuple(attempts[0])[:3],
            (attempt_id, "rejected_revision", failed_at),
        )
        self.assertGreaterEqual(attempts[1][3], failed_at + 1.0)
        requests = self.conn.execute(
            "SELECT r.id,r.state,r.from_line,r.through_line,q.request_id "
            "FROM review_request r LEFT JOIN review_receipt q "
            "ON q.request_id=r.id JOIN review_control k "
            "ON k.policy_digest=r.policy_digest "
            "ORDER BY r.from_line,r.through_line"
        ).fetchall()
        self.assertEqual(
            [(r[2], r[3]) for r in requests],
            [(1, 2), (1, 3), (2, 2), (2, 3), (3, 3)],
        )
        self.assertTrue(
            all(r[1] == "completed" and r[0] == r[4] for r in requests)
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_call c "
                "JOIN review_attempt a ON a.id=c.attempt_id "
                "JOIN review_request r ON r.id=a.request_id "
                "JOIN review_control k ON k.policy_digest=r.policy_digest "
                "WHERE c.state='completed'"
            ).fetchone()[0],
            6,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request r JOIN review_control k "
                "ON r.policy_digest!=k.policy_digest WHERE r.state='pending'"
            ).fetchone()[0],
            1,
        )

    def test_replacement_owner_stops_before_fixture_cleanup(self) -> None:
        owner = self.supervisor
        thread = owner.monitor._thread

        def check_released() -> None:
            try:
                self.assertIs(owner, self.supervisor)
                self.assertEqual(owner.home, self.home)
                self.assertTrue(owner.home.exists())
                self.assertFalse(thread.is_alive())
                self.assertIsNone(owner._owner)
                self.assertTrue(platform_lock.released(owner.home))
            finally:
                owner.stop(20.0)

        self.addCleanup(check_released)
        try:
            self.test_four_workers_claim_live_work_before_older_history()
        finally:
            owner = self.supervisor
            thread = owner.monitor._thread


class BoundedRetryTests(RequestCase):
    """Count total attempts and keep unrelated requests available."""

    def test_third_temporary_failure_stops_only_its_request(self) -> None:
        self.policy = self.policy.model_copy(
            update={"max_attempts": 3, "retry_backoff_seconds": [0.0, 0.0]}
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        request = self.enqueue()
        for index, expected in enumerate(("waiting", "waiting", "failed")):
            attempt = self.transaction(
                lambda index=index: review_requests.claim_attempt(
                    self.conn, request.id, 1, 12.0 + index * 2
                )
            )
            failed = self.transaction(
                lambda prior=attempt, step=index: review_requests.fail_attempt(
                    self.conn, prior.id, "deadline", 13.0 + step * 2
                )
            )
            self.assertEqual(failed.state, expected)
        other = self.enqueue(self.window(3, 3))
        self.assertEqual(
            self.transaction(
                lambda: review_requests.claim_attempt(
                    self.conn, other.id, 1, 14.0
                )
            ).request_id,
            other.id,
        )
