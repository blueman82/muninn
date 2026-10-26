"""Reproduce estimate errors with real durable call reservations."""

from __future__ import annotations

import json
import time

from muninn import review_budget, review_estimate, review_requests
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext
from tests.test_review_requests import RequestCase


class HistoryEstimateTests(RequestCase):
    """Compare JSON identities and account for batches and retries."""

    def completed_call(self, canonical: bool = False) -> None:
        """Reserve a real call and supply fixed observed timing and cost."""
        self.policy = self.policy.model_copy(
            update={
                "concurrency": 4,
                "max_attempts": 3,
                "retry_backoff_seconds": [5.0, 30.0],
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        context = ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=self.scope,
            policy=self.policy,
            admitted_windows=(request.window,),
            evidence_ids=(),
            round_index=1,
            deadline=time.monotonic() + 60.0,
        )
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 13.0)
        )
        usage = {
            "input_tokens": 10,
            "output_tokens": 1,
            "cached_tokens": None,
            "cost_decimal": "1.0",
            "currency": "USD",
        }
        self.conn.execute(
            "UPDATE review_call SET "
            "state='completed',launched_at=100.0,ended_at=200.0,usage_json=?",
            (json.dumps(usage),),
        )
        if canonical:
            self.conn.execute(
                "UPDATE review_call SET requested_json=?",
                (canonical_json(self.policy.runner),),
            )

    def test_real_reservation_json_contributes_to_estimates(self) -> None:
        self.completed_call()
        estimate = review_estimate.estimate(self.conn, self.policy, 1)
        self.assertEqual(estimate["sample_calls"], 1)
        self.assertEqual(estimate["seconds_low"], 100.0)

    def test_single_request_cannot_use_four_workers_at_once(self) -> None:
        self.completed_call(canonical=True)
        estimate = review_estimate.estimate(self.conn, self.policy, 1)
        self.assertEqual(estimate["seconds_low"], 100.0)
        self.assertEqual(estimate["seconds_high"], 200.0)
        self.assertEqual(estimate["retry_seconds_high"], 635.0)
        self.assertEqual(estimate["retry_api_cost_decimal"], "6.0")
        self.assertIn("retries", str(estimate["time_note"]))

    def test_unlaunched_reservations_are_not_timing_samples(self) -> None:
        self.completed_call()
        self.conn.execute(
            "UPDATE review_call SET outcome='not_launched',"
            "launched_at=NULL,usage_json=NULL"
        )
        estimate = review_estimate.estimate(self.conn, self.policy, 1)
        self.assertEqual(estimate["sample_calls"], 0)
        self.assertIsNone(estimate["seconds_low"])
