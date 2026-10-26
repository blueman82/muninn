"""Check bounded automatic recovery and safe explanations."""

from __future__ import annotations

from unittest import mock

from muninn import review_recovery
from muninn.review_models import ReviewPolicy
from tests.test_review_supervisor import SupervisorCase
from tools.prepare_codex_policy import policy


class AutomaticRecoveryTests(SupervisorCase):
    """Exercise dependency recovery without running a provider CLI."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(
            review_recovery, "enabled", return_value=True
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_policy_revision_three_has_bounded_parallel_retries(self) -> None:
        selected, _ = policy(
            self.home, self.home / "codex", "owner", 1.0, revision=3
        )
        self.assertEqual(selected.concurrency, 4)
        self.assertEqual(selected.max_attempts, 3)
        self.assertEqual(len(selected.retry_backoff_seconds), 2)
        with self.assertRaises(ValueError):
            ReviewPolicy.model_validate(
                selected.model_dump() | {"concurrency": 5}
            )
        for revision in (1, 2):
            old, _ = policy(
                self.home, self.home / "codex", "owner", 1.0, revision=revision
            )
            self.assertEqual((old.concurrency, old.max_attempts), (1, 1))

    def test_shared_setup_recovers_without_new_attempt(self) -> None:
        with mock.patch.object(
            self.supervisor,
            "_create",
            side_effect=ValueError("effective_host_changed"),
        ):
            self.supervisor.tick(13.0)
        issue = review_recovery.notice(self.conn)
        self.assertIsNotNone(issue)
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.supervisor.tick(80.0)
        self.finish()
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.assertIsNone(review_recovery.notice(self.conn))

    def test_owner_pause_survives_dependency_recheck(self) -> None:
        with mock.patch.object(
            self.supervisor,
            "_create",
            side_effect=ValueError("effective_host_changed"),
        ):
            self.supervisor.tick(13.0)
        self.conn.execute("UPDATE review_control SET admission=0")
        with mock.patch.object(self.supervisor, "_create") as create:
            self.supervisor.tick(80.0)
        create.assert_not_called()
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )

    def test_explanations_do_not_echo_unknown_exception_text(self) -> None:
        issue = review_recovery.explain("secret transcript text", "launch")
        self.assertNotIn("secret", str(issue))
        self.assertIn("cause", issue)
        self.assertIn("action", issue)
