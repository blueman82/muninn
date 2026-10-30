"""Require explicit consent for contained legacy learning recovery."""

from __future__ import annotations

import time
from unittest import mock

from install import (
    learning_scope,
    review_policy_default,
    review_policy_transition,
)
from install.context import Ctx, StepFailedError
from muninn import review_history, review_requests, store
from muninn.review_budget import reserve_call
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext, ReviewPolicy
from tests.ingest_support import line
from tests.test_review_requests import RequestCase
from tools.prepare_codex_policy import DEFAULT_POLICY_FILE, policy


class LearningConsentTests(RequestCase):
    """Keep custom settings and owner pauses behind explicit approval."""

    def setUp(self) -> None:
        super().setUp()
        with store.writer_lock(self.home):
            self.assertEqual(
                self.conn.execute("PRAGMA user_version").fetchone()[0], 2
            )

    def test_missing_current_lock_refuses_without_changing_state(self) -> None:
        self.legacy()
        self.conn.execute("UPDATE review_control SET admission=0")
        (self.home / "writer.lock").unlink()
        before = store.db_path(self.home).read_bytes()
        ctx = self.context()
        ctx.enable_learning = True
        with self.assertRaisesRegex(
            StepFailedError, "review_policy_preview_unavailable: state_missing"
        ):
            learning_scope.choose(ctx, "now", lambda question: "yes")
        self.assertEqual(store.db_path(self.home).read_bytes(), before)
        self.assertFalse((self.home / "writer.lock").exists())
        self.assertIsNone(ctx.learning_mode)

    def context(self) -> Ctx:
        """Use a disposable installer context without service operations."""
        ctx = Ctx(
            self.tmp,
            mock.Mock(),
            "consent",
            platform="darwin",
            upgrade=True,
            now=lambda: 30.0,
        )
        ctx.data = self.home
        ctx.say = mock.Mock()
        return ctx

    def legacy(self, default: bool = False) -> None:
        """Build the exact old profile with its recorded runner version."""
        old, _ = policy(
            self.home,
            self.home / "codex",
            "owner",
            1.0,
            revision=2,
            repository_default=default,
        )
        self.policy = old.model_copy(
            update={
                "runner": old.runner.model_copy(
                    update={"version": "recorded-version"}
                )
            }
        )
        self.conn.execute(
            "UPDATE review_control SET "
            "admission=1,policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def replacement(self) -> ReviewPolicy:
        """Build the approved successor while preserving runner identity."""
        new, _ = policy(
            self.home,
            self.home / "codex",
            "new approval",
            2.0,
            revision=3,
            repository_default=True,
        )
        return new.model_copy(update={"runner": self.policy.runner})

    def test_declining_keeps_the_existing_pause(self) -> None:
        self.legacy()
        self.conn.execute("UPDATE review_control SET admission=0")
        ctx = self.context()
        learning_scope.choose(ctx, None, lambda question: "no")
        self.assertFalse(ctx.enable_learning)
        self.assertIsNone(ctx.learning_mode)
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )

    def test_accepting_legacy_recovery_sets_a_separate_consent(self) -> None:
        self.legacy()
        self.conn.execute("UPDATE review_control SET admission=0")
        ctx = self.context()
        learning_scope.choose(ctx, "all", lambda question: "yes")
        self.assertTrue(ctx.enable_learning)
        self.assertEqual(ctx.learning_mode, "all")
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )

    def test_explicit_transition_preserves_failed_and_pending_history(
        self,
    ) -> None:
        self.legacy()
        failed = self.enqueue(self.window(2, 2))
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, failed.id, 1, 12.0
            )
        )
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, attempt.id, "config", 13.0
            )
        )
        pending = self.enqueue(self.window(3, 3))
        self.conn.execute("UPDATE review_control SET admission=0")
        old_json = canonical_json(self.policy)
        review_policy_transition.apply(
            self.conn, self.replacement(), enable_admission=True
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_json FROM review_request WHERE id=?",
                (pending.id,),
            ).fetchone()[0],
            old_json,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM review_request WHERE id=?", (failed.id,)
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT cancel_code FROM review_request WHERE id=?",
                (pending.id,),
            ).fetchone()[0],
            "policy_replaced",
        )

    def test_custom_limits_are_not_silently_promoted(self) -> None:
        self.legacy()
        self.policy = self.policy.model_copy(update={"context_bytes": 8000})
        self.conn.execute(
            "UPDATE review_control SET "
            "admission=0,policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        ctx = self.context()
        ctx.enable_learning = True
        with self.assertRaises(StepFailedError):
            learning_scope.choose(ctx, "now", lambda question: "yes")

    def test_saved_all_scope_replaces_cancelled_old_policy_work(self) -> None:
        self.legacy(default=True)
        selected = review_history.scan(self.conn, self.policy, None)
        self.transaction(
            lambda: review_history.configure(
                self.conn, self.policy, "all", None, 20.0, selected.digest
            )
        )
        ctx = self.context()
        learning_scope.choose(ctx, None, lambda question: "yes")
        self.assertEqual(ctx.learning_mode, "all")
        review_policy_transition.apply(self.conn, self.replacement())
        learning_scope.apply(ctx, {}, self.conn)
        count = self.conn.execute(
            "SELECT count(*) FROM review_request WHERE "
            "state='pending' AND policy_digest=?",
            (self.digest(self.replacement()),),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_default_selection_freezes_legacy_version_and_explicit_grant(
        self,
    ) -> None:
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
        ):
            self.legacy()
            self.conn.execute("UPDATE review_control SET admission=0")
            ctx = self.context()
            ctx.default_home = True
            ctx.enable_learning = True
            ctx.learning_mode = "all"
            executable = self.home / "codex"
            executable.write_text("signed test executable")
            executable.chmod(0o700)
            ctx.codex_home.mkdir(mode=0o700)
            with (
                mock.patch(
                    "muninn.review_codex_launch.selected_auth_home",
                    return_value=ctx.codex_home,
                ),
                mock.patch(
                    "muninn.review_codex_launch.verify_codex_executable",
                    return_value=(executable, "1" * 64),
                ),
            ):
                selected = review_policy_default.select(
                    ctx, DEFAULT_POLICY_FILE.read_bytes()
                )
            assert selected is not None
            chosen, record = selected
            self.assertEqual(chosen.runner, self.policy.runner)
            self.assertEqual(
                chosen.policy_id, "muninn-codex-ongoing-production-v3"
            )
            self.assertEqual(chosen.budget.approved_at, 30.0)
            rebuilt, _ = policy(
                self.home,
                executable,
                chosen.budget.approved_by,
                chosen.budget.approved_at,
                revision=3,
                repository_default=True,
                profile_raw=record["default_profile"].encode(),
            )
            self.assertEqual(rebuilt, chosen)
            self.assertEqual(
                self.conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )

    def test_explicit_restart_refuses_an_unfinished_attempt(self) -> None:
        self.legacy()
        request = self.enqueue()
        self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.conn.execute("UPDATE review_control SET admission=0")
        ctx = self.context()
        ctx.enable_learning = True
        with self.assertRaisesRegex(
            StepFailedError, "learning_recovery_unverified"
        ):
            learning_scope.choose(ctx, "now", lambda question: "yes")
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )

    def test_saved_future_scope_keeps_queued_post_boundary_work(self) -> None:
        self.legacy(default=True)
        self.transaction(
            lambda: review_history.configure(
                self.conn, self.policy, "now", None, 20.0, None
            )
        )
        self.records.extend(self.records[1:])
        self.path.write_bytes(b"".join(line(item) for item in self.records))
        self.run_ingest()
        self.transaction(
            lambda: review_requests.enqueue_review(
                self.conn,
                self.window(4, 5),
                self.scope,
                self.policy,
                "hook",
                21.0,
            )
        )
        ctx = self.context()
        learning_scope.choose(ctx, None, lambda question: "yes")
        review_policy_transition.apply(self.conn, self.replacement())
        learning_scope.apply(ctx, {}, self.conn)
        count = self.conn.execute(
            "SELECT count(*) FROM review_request WHERE "
            "state='pending' AND policy_digest=?",
            (self.digest(self.replacement()),),
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT selected_at FROM learning_setup"
            ).fetchone()[0],
            20.0,
        )

    def test_uncertain_call_blocks_explicit_restart_after_attempt_ends(
        self,
    ) -> None:
        self.legacy()
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
            deadline=time.monotonic() + 120.0,
        )
        self.transaction(lambda: reserve_call(self.conn, context, 13.0))
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, attempt.id, "deadline", 14.0
            )
        )
        self.conn.execute("UPDATE review_control SET admission=0")
        with self.assertRaisesRegex(
            StepFailedError, "learning_recovery_unverified"
        ):
            review_policy_transition.apply(
                self.conn, self.replacement(), enable_admission=True
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_call").fetchone()[0],
            "unknown",
        )

    def test_same_revision_can_restart_only_with_explicit_consent(
        self,
    ) -> None:
        self.legacy()
        selected = self.replacement()
        review_policy_transition.apply(self.conn, selected)
        self.conn.execute("UPDATE review_control SET admission=0")
        review_policy_transition.apply(self.conn, selected)
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )
        review_policy_transition.apply(
            self.conn, selected, enable_admission=True
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )
