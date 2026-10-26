"""Exercise real parallel wrappers and bounded request retries."""

from __future__ import annotations

import time

from muninn import review_requests, review_supervisor
from muninn.review_json import canonical_json
from tests.test_review_requests import RequestCase
from tests.test_review_supervisor import SupervisorCase


class ParallelReviewTests(SupervisorCase):
    """Use fake provider processes with actual main-thread coordination."""

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
        until = time.monotonic() + 12.0
        while time.monotonic() < until:
            self.supervisor.tick(16.0)
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
