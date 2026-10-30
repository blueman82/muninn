"""Check bounded automatic recovery and safe explanations."""

from __future__ import annotations

import time
from unittest import mock

from muninn import review_recovery, review_supervisor
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

    def test_clean_stop_retires_only_proven_unlaunched_reservation(
        self,
    ) -> None:
        with mock.patch.object(
            self.supervisor,
            "_create",
            side_effect=ValueError("effective_host_changed"),
        ):
            self.supervisor.tick(13.0)
        self.supervisor.stop(14.0)
        row = self.conn.execute(
            "SELECT state,outcome FROM review_call"
        ).fetchone()
        self.assertEqual(tuple(row), ("completed", "not_launched"))
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )

    def test_missing_launch_files_hold_queue_until_restored(self) -> None:
        self.enqueue(self.window(1, 2))
        codes = (
            "capability_unavailable",
            "capability_invalid",
            "capability_path",
            "capability_storage",
            "capability_executable_storage",
            "capability_executable_provenance",
            "capability_platform",
            "capability_limit",
            "codex_experimental_boundary",
            "codex_approval_required",
            "codex_data_home_changed",
            "codex_capability_status",
            "codex_platform_changed",
            "codex_host_changed",
            "codex_project_required",
            "native_prompt_instruction",
            "launch_input_platform",
            "launch_input_storage",
            "runner_unsupported",
        )
        for index, code in enumerate(codes):
            with (
                self.subTest(code=code),
                mock.patch.object(
                    review_supervisor,
                    "prepare_launch",
                    side_effect=ValueError(code),
                ),
            ):
                self.supervisor.tick(13.0 + index * 61.0)
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM review_request "
                        "WHERE state IN ('running','pending')"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM review_call"
                    ).fetchone()[0],
                    0,
                )
                issue = review_recovery.notice(self.conn)
                self.assertIsNotNone(issue)
                assert issue is not None
                self.assertEqual(issue["code"], code)
        until = time.monotonic() + 8.0
        while time.monotonic() < until:
            self.supervisor.tick(1500.0)
            if (
                self.conn.execute(
                    "SELECT count(*) FROM review_receipt"
                ).fetchone()[0]
                == 2
            ):
                break
            time.sleep(0.01)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_receipt"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            2,
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
