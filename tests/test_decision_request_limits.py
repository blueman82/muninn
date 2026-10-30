"""Preserve normal capacity, retries and recovery for application requests."""

from __future__ import annotations

from functools import partial

from muninn import decision_requests, review_requests
from muninn.decision_write import submit_intent
from muninn.review_supervisor import Supervisor
from tests.decision_support import DecisionCase


class DecisionRequestLimitTests(DecisionCase):
    """Exercise historic attempts without simulating successful application."""

    def submit(self, ident: str) -> None:
        """Submit a physically backed locator-only approval."""
        intent = self.intent(ident)
        self.transaction(lambda: submit_intent(self.conn, intent, 20.0))

    def bounded_policy(self) -> None:
        """Use production worker, attempt and retry ceilings in fixtures."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 20.0
            )
        )
        self.policy = self.policy.model_copy(
            update={
                "concurrency": 4,
                "max_attempts": 3,
                "retry_backoff_seconds": [5.0, 30.0],
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )
        self.context = self.start()

    def test_four_shared_workers_prevent_a_fifth_application_claim(
        self,
    ) -> None:
        self.bounded_policy()
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        for number in (2, 3, 4):
            request = self.enqueue(self.window(number, number))
            self.transaction(
                partial(
                    review_requests.claim_attempt,
                    self.conn,
                    request.id,
                    1,
                    25.0,
                )
            )
        before = self.row("SELECT count(*) FROM review_attempt")[0]
        view = decision_requests.request_application(
            self.home, candidate.id, 26.0
        )
        self.assertEqual(view.state, "approved_pending")
        application = self.row(
            "SELECT application_request_id FROM memory_candidate WHERE id=?",
            (candidate.id,),
        )[0]
        with self.assertRaisesRegex(ValueError, "review_concurrency"):
            self.transaction(
                lambda: review_requests.claim_attempt(
                    self.conn, application, 1, 27.0
                )
            )
        self.assertEqual(
            self.row("SELECT count(*) FROM review_attempt")[0], before
        )
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.transaction(
                lambda: review_requests.claim_proposal(
                    self.conn, application, 1, 27.0
                )
            )

    def test_application_recovery_preserves_backoff_identity_and_attempts(
        self,
    ) -> None:
        self.bounded_policy()
        candidate = self.stage()
        self.submit(candidate.id)
        decision_requests.request_application(self.home, candidate.id, 25.0)
        application = self.row(
            "SELECT application_request_id FROM memory_candidate WHERE id=?",
            (candidate.id,),
        )[0]
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, application, 1, 26.0
            )
        )
        self.conn.execute("UPDATE review_control SET generation=2")
        self.transaction(
            lambda: review_requests.recover_attempts(self.conn, 2, 27.0)
        )
        row = self.row(
            "SELECT state,retry_at FROM review_request WHERE id=?",
            (application,),
        )
        self.assertEqual(tuple(row), ("waiting", 32.0))
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT ended_at,outcome FROM review_attempt WHERE id=?",
                    (attempt.id,),
                )
            ),
            (27.0, "abandoned"),
        )
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 28.0
            )
        )
        supervisor = Supervisor(self.home, self.policy)
        supervisor.generation = 2
        self.assertIsNone(supervisor._claim(33.0))
        decision_requests.continue_decisions(self.home, 2, 33.0)
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_attempt WHERE request_id=?",
                (application,),
            )[0],
            1,
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 0)

    def test_application_cannot_bypass_three_attempts_with_operator_retry(
        self,
    ) -> None:
        self.bounded_policy()
        candidate = self.stage()
        self.submit(candidate.id)
        decision_requests.request_application(self.home, candidate.id, 25.0)
        application = self.row(
            "SELECT application_request_id FROM memory_candidate WHERE id=?",
            (candidate.id,),
        )[0]
        for started, ended, expected in (
            (30.0, 31.0, 36.0),
            (36.0, 37.0, 67.0),
            (67.0, 68.0, None),
        ):
            attempt = self.transaction(
                partial(
                    review_requests.claim_attempt,
                    self.conn,
                    application,
                    1,
                    started,
                )
            )
            failed = self.transaction(
                partial(
                    review_requests.fail_attempt,
                    self.conn,
                    attempt.id,
                    "deadline",
                    ended,
                )
            )
            self.assertEqual(failed.retry_at, expected)
            if expected is not None:
                with self.assertRaisesRegex(ValueError, "request_not_ready"):
                    self.transaction(
                        partial(
                            review_requests.claim_attempt,
                            self.conn,
                            application,
                            1,
                            expected - 1.0,
                        )
                    )
        self.transaction(
            lambda: review_requests.retry_request(self.conn, application, 70.0)
        )
        with self.assertRaisesRegex(ValueError, "review_attempt_limit"):
            self.transaction(
                lambda: review_requests.claim_attempt(
                    self.conn, application, 1, 71.0
                )
            )
        decision_requests.request_application(self.home, candidate.id, 72.0)
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_attempt WHERE request_id=?",
                (application,),
            )[0],
            3,
        )
