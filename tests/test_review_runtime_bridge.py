"""Check one finite old queue bridge without rewriting request snapshots."""

from __future__ import annotations

import sqlite3
import time
from unittest import mock

from install import review_policy_transition
from muninn import review_budget, review_claims, review_prompt, review_rounds
from muninn import review_runtime_bridge as bridge
from muninn.review_commit_state import checked_request_policy
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext
from muninn.review_read_budget import ReadBudget
from muninn.review_supervisor_store import mark_correction
from tests.managed_codex_support import identity, publish_authority, selection
from tests.test_review_codex_production import experimental_policy
from tests.test_review_requests import RequestCase


class RuntimeBridgeTests(RequestCase):
    """Use queue rows and private content-addressed approval fixtures."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = experimental_policy(self.home)
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.request = self.enqueue()
        self.old = self.policy
        self.new = self.old.model_copy(
            update={"runner": selection(self.tmp / "manager")}
        )
        capsule = bridge.capture(self.conn, self.old, self.new)
        publish_authority(self.home, self.old, self.new, capsule)
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.new), self.digest(self.new)),
        )

    def row(self) -> sqlite3.Row:
        """Reload the immutable old snapshot on the existing connection."""
        return self.conn.execute(
            "SELECT * FROM review_request WHERE id=?", (self.request.id,)
        ).fetchone()

    def test_exact_old_request_claims_under_one_current_selection(
        self,
    ) -> None:
        raw = self.row()["policy_json"]
        self.assertEqual(
            bridge.effective_policy(self.conn, self.row()), self.new
        )
        attempt = self.transaction(
            lambda: review_claims.claim(
                self.conn,
                self.request.id,
                1,
                12.0,
                proposal=False,
            )
        )
        self.assertEqual(attempt.requested, self.new.runner)
        self.assertEqual(self.row()["policy_json"], raw)
        self.assertEqual(self.row()["policy_digest"], self.digest(self.old))

    def test_changed_scope_or_policy_terms_refuse(self) -> None:
        self.conn.execute(
            "UPDATE review_request SET scope_json='{}' WHERE id=?",
            (self.request.id,),
        )
        with self.assertRaisesRegex(ValueError, "review_policy_required"):
            bridge.effective_policy(self.conn, self.row())

    def test_removed_authority_refuses_without_reconstruction(self) -> None:
        for path in (
            self.home / "review-capabilities" / "managed-approvals"
        ).iterdir():
            path.unlink()
        with self.assertRaises(ValueError):
            bridge.effective_policy(self.conn, self.row())

    def test_same_bridge_reaches_refresh_read_reservation_and_correction(
        self,
    ) -> None:
        attempt = self.transaction(
            lambda: review_claims.claim(
                self.conn,
                self.request.id,
                1,
                12.0,
                proposal=False,
            )
        )
        context = ReviewContext(
            request_id=self.request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=self.request.scope,
            policy=self.new,
            admitted_windows=(self.request.window,),
            evidence_ids=(),
            round_index=1,
            deadline=time.monotonic() + 100,
        )
        self.assertEqual(review_prompt.refresh(self.home, context), context)
        review_rounds._current(
            self.conn, context, ReadBudget(self.new.max_input_bytes), None
        )
        call = self.transaction(
            lambda: review_budget.reserve_call(
                self.conn,
                context,
                13.0,
                execution=identity(self.tmp / "manager"),
            )
        )
        self.assertEqual(call.policy_digest, self.digest(self.new))
        self.assertEqual(
            checked_request_policy(self.conn, self.row()), self.new
        )
        self.conn.execute(
            "UPDATE review_call SET state='completed',ended_at=14.0"
        )
        self.transaction(lambda: mark_correction(self.conn, context, "format"))
        self.assertEqual(
            self.conn.execute("SELECT outcome FROM review_call").fetchone()[0],
            "correction_format_requested",
        )
        self.conn.execute("UPDATE review_control SET generation=2")
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.transaction(
                lambda: mark_correction(self.conn, context, "format")
            )

    def test_explicit_managed_restart_keeps_old_queue_and_exact_grant(
        self,
    ) -> None:
        self.conn.execute("UPDATE review_control SET admission=0")
        before = self.row()["policy_json"]
        with (
            mock.patch(
                "muninn.review_codex_policy.host_identity",
                return_value="1" * 64,
            ),
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
        ):
            review_policy_transition.apply(
                self.conn, self.new, enable_admission=True
            )
        self.assertEqual(self.row()["state"], "pending")
        self.assertEqual(self.row()["policy_json"], before)
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_digest,admission FROM review_control"
            ).fetchone()[:],
            (self.digest(self.new), 1),
        )

    def test_revision_three_restart_does_not_cancel_initial_carried_queue(
        self,
    ) -> None:
        self.conn.execute("DELETE FROM review_request")
        self.old = self.old.model_copy(
            update={
                "policy_id": "muninn-codex-ongoing-production-v3",
                "max_attempts": 3,
                "retry_backoff_seconds": [5.0, 30.0],
                "concurrency": 4,
            }
        )
        self.policy = self.old
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,"
            "policy_digest=?,admission=1",
            (canonical_json(self.old), self.digest(self.old)),
        )
        self.request = self.enqueue()
        self.new = self.old.model_copy(
            update={"runner": selection(self.tmp / "manager")}
        )
        capsule = bridge.capture(self.conn, self.old, self.new)
        publish_authority(self.home, self.old, self.new, capsule)
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,"
            "policy_digest=?,admission=0",
            (canonical_json(self.new), self.digest(self.new)),
        )
        with (
            mock.patch(
                "muninn.review_codex_policy.host_identity",
                return_value="1" * 64,
            ),
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
        ):
            review_policy_transition.apply(
                self.conn, self.new, enable_admission=True
            )
        self.assertEqual(self.row()["state"], "pending")
        self.assertIsNone(self.row()["cancel_code"])
