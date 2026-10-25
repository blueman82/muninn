"""A revised ongoing read allowance has a narrow durable transition."""

from __future__ import annotations

from pathlib import Path

from install import review_policy_transition
from install.context import StepFailedError
from muninn import review_progress, store
from muninn.review_json import canonical_json
from muninn.review_progress import digest
from tests.store_support import StoreCase
from tools.prepare_codex_policy import policy


class OngoingReadTransitionTests(StoreCase):
    """Keep an existing activation while changing only its raw-read cap."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(store.writer_lock(self.home, wait_s=0))
        self.conn = self.rw()
        self.old, _ = policy(self.home, Path("/fake-codex"), "owner", 10.0)
        self.new, _ = policy(
            self.home, Path("/fake-codex"), "owner", 11.0, revision=2
        )
        self.default_old, _ = policy(
            self.home,
            Path("/fake-codex"),
            "owner",
            10.0,
            repository_default=True,
        )
        self.default_new, _ = policy(
            self.home,
            Path("/fake-codex"),
            "owner",
            11.0,
            revision=2,
            repository_default=True,
        )
        self.conn.execute("BEGIN IMMEDIATE")
        review_progress.bootstrap_policy(self.conn, self.old, 20.0)
        self.conn.execute("COMMIT")

    def test_revision_preserves_activation_and_fences_generation(self) -> None:
        before = tuple(
            self.conn.execute(
                "SELECT admission,generation,activated_at FROM review_control"
            ).fetchone()
        )
        review_policy_transition.check(self.conn, self.old, self.new)
        review_policy_transition.apply(self.conn, self.new)
        after = tuple(
            self.conn.execute(
                "SELECT admission,generation,activated_at,policy_digest "
                "FROM review_control"
            ).fetchone()
        )
        self.assertEqual(
            after, (before[0], before[1] + 1, before[2], digest(self.new))
        )

    def test_unrelated_policy_change_refuses(self) -> None:
        changed = self.new.model_copy(update={"prompt_digest": "1" * 64})
        with self.assertRaisesRegex(
            StepFailedError, "review_policy_transition_required"
        ):
            review_policy_transition.check(self.conn, self.old, changed)

    def test_prior_approval_and_existing_knowledge_refuse(self) -> None:
        stale = self.new.model_copy(
            update={
                "budget": self.new.budget.model_copy(
                    update={"approved_at": 10.0}
                )
            }
        )
        with self.assertRaisesRegex(
            StepFailedError, "review_policy_transition_required"
        ):
            review_policy_transition.check(self.conn, self.old, stale)
        self.conn.execute(
            "INSERT INTO knowledge VALUES(?,1,'active',NULL,NULL)",
            ("K_" + "1" * 32,),
        )
        with self.assertRaisesRegex(
            StepFailedError, "review_policy_transition_required"
        ):
            review_policy_transition.check(self.conn, self.old, self.new)

    def test_installed_duplicate_json_key_refuses(self) -> None:
        raw = canonical_json(self.old)
        duplicate = '{"policy_id":"forged",' + raw[1:]
        self.conn.execute(
            "UPDATE review_control SET policy_json=?", (duplicate,)
        )
        with self.assertRaisesRegex(StepFailedError, "review_policy_invalid"):
            review_policy_transition.compatible(self.conn, self.new)

    def test_repository_default_refresh_preserves_admission_and_changes_bounds(
        self,
    ) -> None:
        """Allow a bounded default refresh before learning has been used."""
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=? "
            "WHERE singleton=1",
            (canonical_json(self.default_old), digest(self.default_old)),
        )
        generation = self.conn.execute(
            "SELECT generation FROM review_control"
        ).fetchone()[0]
        review_policy_transition.check(
            self.conn, self.default_old, self.default_new
        )
        review_policy_transition.apply(self.conn, self.default_new)
        row = self.conn.execute(
            "SELECT admission,generation,policy_digest FROM review_control"
        ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], generation + 1)
        self.assertEqual(row[2], digest(self.default_new))

    def test_repository_default_refresh_refuses_custom_or_disabled_policy(
        self,
    ) -> None:
        """Do not refresh custom or opted-out policies."""
        with self.assertRaisesRegex(
            StepFailedError, "review_policy_transition_required"
        ):
            review_policy_transition.check(
                self.conn,
                self.default_old,
                self.default_new.model_copy(update={"policy_source": None}),
            )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?,"
            "admission=0 WHERE singleton=1",
            (canonical_json(self.default_old), digest(self.default_old)),
        )
        with self.assertRaisesRegex(
            StepFailedError, "review_policy_transition_required"
        ):
            review_policy_transition.check(
                self.conn, self.default_old, self.default_new
            )
