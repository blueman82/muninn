"""Check one durable charge and immutable per-call candidate history."""

from __future__ import annotations

from muninn import review_budget, review_requests
from muninn import review_runtime_records as records
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext
from muninn.review_runtime_models import ManagedCallRecord
from tests.managed_codex_support import identity, selection
from tests.test_review_codex_production import experimental_policy
from tests.test_review_requests import RequestCase


class ManagedRecordTests(RequestCase):
    """Exercise reservations on a real temporary indexed store."""

    def setUp(self) -> None:
        super().setUp()
        self.selected = selection(self.tmp / "manager")
        self.policy = experimental_policy(self.home).model_copy(
            update={"runner": self.selected}
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def context(self) -> ReviewContext:
        """Claim under the stable policy with normal live control fences."""
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn,
                request.id,
                1,
                12.0,
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
            deadline=100.0,
        )

    def test_reservation_requires_and_retains_actual_candidate(self) -> None:
        context = self.context()
        observed = identity(self.selected.manager_root)
        call = self.transaction(
            lambda: review_budget.reserve_call(
                self.conn,
                context,
                13.0,
                execution=observed,
            )
        )
        duplicate = self.transaction(
            lambda: review_budget.reserve_call(
                self.conn,
                context,
                14.0,
                execution=observed,
            )
        )
        self.assertEqual(call, duplicate)
        assert isinstance(call.requested, ManagedCallRecord)
        self.assertEqual(call.requested.selection, self.selected)
        self.assertEqual(call.requested.candidates, (observed,))
        self.assertIsNone(call.requested.launched_identity)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*),min(reserved_at) FROM review_call"
            ).fetchone()[:],
            (1, 13.0),
        )

    def test_offer_refresh_and_launch_are_append_only(self) -> None:
        context = self.context()
        first = identity(self.selected.manager_root)
        second = identity(self.selected.manager_root, "0.167.0")
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn,
                context,
                13.0,
                execution=first,
            )
        )
        self.transaction(
            lambda: records.offer_execution(self.conn, context, first, 14.0)
        )
        self.transaction(
            lambda: records.append_prestart_refusal(
                self.conn,
                context,
                records.identity_digest(first),
                "exec_enoent",
                15.0,
            )
        )
        self.transaction(
            lambda: records.offer_execution(self.conn, context, second, 16.0)
        )
        self.transaction(
            lambda: records.freeze_launched_execution(
                self.conn,
                context,
                records.identity_digest(second),
                17.0,
            )
        )
        row = self.conn.execute("SELECT * FROM review_call").fetchone()
        call = review_budget.call_view(row)
        assert isinstance(call.requested, ManagedCallRecord)
        self.assertEqual(call.requested.candidates, (first, second))
        self.assertEqual(call.requested.refresh_count, 1)
        self.assertEqual(
            call.requested.launched_identity, records.identity_digest(second)
        )
        before = row["requested_json"]
        with self.assertRaisesRegex(ValueError, "runtime_race_exhausted"):
            self.transaction(
                lambda: records.offer_execution(
                    self.conn, context, first, 18.0
                )
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT requested_json FROM review_call"
            ).fetchone()[0],
            before,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_second_replacement_and_unoffered_launch_refuse(self) -> None:
        context = self.context()
        first = identity(self.selected.manager_root)
        second = identity(self.selected.manager_root, "0.167.0")
        third = identity(self.selected.manager_root, "0.168.0")
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, context, 13.0, execution=first
            )
        )
        with self.assertRaisesRegex(ValueError, "runtime_race_exhausted"):
            self.transaction(
                lambda: records.freeze_launched_execution(
                    self.conn,
                    context,
                    records.identity_digest(first),
                    14.0,
                )
            )
        self.transaction(
            lambda: records.offer_execution(self.conn, context, second, 15.0)
        )
        with self.assertRaisesRegex(ValueError, "runtime_race_exhausted"):
            self.transaction(
                lambda: records.offer_execution(
                    self.conn, context, third, 16.0
                )
            )

    def test_actual_launch_observation_survives_later_authority_fence(
        self,
    ) -> None:
        context = self.context()
        observed = identity(self.selected.manager_root)
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, context, 13.0, execution=observed
            )
        )
        self.transaction(
            lambda: records.offer_execution(self.conn, context, observed, 14.0)
        )
        self.conn.execute("UPDATE review_control SET admission=0,generation=2")
        self.conn.execute(
            "UPDATE review_call SET state='unknown',ended_at=15.0,outcome='ev"
            "idence_erased'"
        )
        self.conn.execute(
            "UPDATE review_attempt SET ended_at=15.0,outcome='evidence_erased'"
        )
        self.transaction(
            lambda: records.freeze_launched_execution(
                self.conn,
                context,
                records.identity_digest(observed),
                16.0,
            )
        )
        row = self.conn.execute("SELECT * FROM review_call").fetchone()
        self.assertEqual(
            (
                row["state"],
                row["ended_at"],
                row["outcome"],
                row["reserved_at"],
            ),
            ("unknown", 15.0, "evidence_erased", 13.0),
        )
        self.assertEqual(
            ManagedCallRecord.model_validate_json(
                row["requested_json"]
            ).launched_identity,
            records.identity_digest(observed),
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_exhausted_runtime_race_is_terminal_without_extra_attempt(
        self,
    ) -> None:
        context = self.context()
        observed = identity(self.selected.manager_root)
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, context, 13.0, execution=observed
            )
        )
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, context.attempt_id, "runtime_race_exhausted", 14.0
            )
        )
        row = self.conn.execute(
            "SELECT state,retry_at FROM review_request"
        ).fetchone()
        self.assertEqual(tuple(row), ("failed", None))
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*),min(reserved_at) FROM review_call"
            ).fetchone()[:],
            (1, 13.0),
        )
