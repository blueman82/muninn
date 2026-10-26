"""Check policy refresh without rewriting historical request authority."""

from __future__ import annotations

from install import review_policy_transition
from muninn.review_json import canonical_json
from tests.test_review_requests import RequestCase
from tools.prepare_codex_policy import policy


class RecoveryPolicyTransitionTests(RequestCase):
    """Preserve immutable records, knowledge, and explicit pause state."""

    def test_default_refresh_replaces_only_unstarted_old_requests(
        self,
    ) -> None:
        old, _ = policy(
            self.home,
            self.home / "codex",
            "owner",
            1.0,
            revision=2,
            repository_default=True,
        )
        new, _ = policy(
            self.home,
            self.home / "codex",
            "owner",
            2.0,
            revision=3,
            repository_default=True,
        )
        self.policy = old
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(old), self.digest(old)),
        )
        request = self.enqueue()
        review_policy_transition.apply(self.conn, new)
        row = self.conn.execute(
            "SELECT * FROM review_request WHERE id=?", (request.id,)
        ).fetchone()
        self.assertEqual(row["policy_json"], canonical_json(old))
        self.assertEqual(row["cancel_code"], "policy_replaced")
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )

    def test_refresh_preserves_a_closed_admission(self) -> None:
        old, _ = policy(
            self.home,
            self.home / "codex",
            "owner",
            1.0,
            revision=2,
            repository_default=True,
        )
        new, _ = policy(
            self.home,
            self.home / "codex",
            "owner",
            2.0,
            revision=3,
            repository_default=True,
        )
        self.conn.execute(
            "UPDATE review_control SET "
            "admission=0,policy_json=?,policy_digest=?",
            (canonical_json(old), self.digest(old)),
        )
        review_policy_transition.apply(self.conn, new)
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )
