"""Exercise durable production reservations and terminal containment state."""

from __future__ import annotations

from muninn import review_budget, review_requests
from muninn.review_models import ReviewContext, Usage
from muninn.review_process import ProcessOutcome
from muninn.review_requests import UNKNOWN_CONFIG
from muninn.review_supervisor_store import halt_interrupted, persist_event
from tests.test_review_codex_production import experimental_policy
from tests.test_review_requests import RequestCase


class CodexLifecycleTests(RequestCase):
    """Keep unknown charges and stop subsequent admission after uncertainty."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = experimental_policy(self.home)
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.context = ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=request.scope,
            policy=self.policy,
            admitted_windows=(request.window,),
            evidence_ids=(),
            round_index=1,
            deadline=132.0,
        )
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, self.context, 13.0)
        )

    def outcome(self, error: str | None, usage: Usage | None = None) -> None:
        """Persist a cleaned synthetic terminal event through the real path."""
        persist_event(
            self.home,
            ProcessOutcome(
                self.context.attempt_id,
                1,
                b"",
                0,
                error,
                1.0,
                UNKNOWN_CONFIG,
                usage,
            ),
            14.0,
        )

    def test_uncertain_output_halts_and_preserves_unknown_call(self) -> None:
        self.outcome("native_tool_event")
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )
        call = review_budget.call_view(
            self.conn.execute("SELECT * FROM review_call").fetchone()
        )
        self.assertEqual(call.state, "unknown")
        self.assertIsNone(call.cost_reservation_decimal)
        self.assertIsNone(call.usage)

    def test_graceful_stop_cannot_hide_an_interrupted_call(self) -> None:
        self.transaction(lambda: halt_interrupted(self.conn, self.policy))
        self.conn.execute("UPDATE review_control SET generation=2")
        self.transaction(
            lambda: review_requests.recover_attempts(self.conn, 2, 15.0)
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )

    def test_reported_cost_is_recorded_without_an_aggregate_ceiling(
        self,
    ) -> None:
        self.outcome(
            None,
            Usage(
                input_tokens=None,
                output_tokens=None,
                cached_tokens=None,
                cost_decimal="200000",
                currency="USD",
            ),
        )
        context = self.context.model_copy(update={"round_index": 2})
        call = self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 1_000_000.0)
        )
        self.assertEqual(call.state, "reserved")
        self.assertIsNone(call.cost_reservation_decimal)
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )
