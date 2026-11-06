"""Check exact fresh consent and an inert stable authority artifact recipe."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from unittest import mock

from install import (
    codex_interface,
    installer,
    learning_scope,
    review_policy,
)
from install.codex_interface_preview import preview
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn.review_progress import digest
from tests.codex_interface_support import InterfaceApplyFixture
from tools.codex_interface_artifacts import plan_artifacts


class InterfaceApprovalTests(InterfaceApplyFixture):
    """Keep engineering approval separate from the owner's rule grant."""

    def test_declined_or_noninteractive_rule_cannot_activate(self) -> None:
        plan = preview(self.ctx)
        for answer in ("", "no", "approved digest"):
            with (
                self.subTest(answer=answer),
                self.assertRaisesRegex(
                    StepFailedError, "codex_interface_approval_required"
                ),
            ):
                codex_interface.confirm(
                    plan, lambda _, answer=answer: answer, 10.0
                )
        with (
            mock.patch("sys.stdin.isatty", return_value=False),
            self.assertRaisesRegex(
                StepFailedError, "codex_interface_approval_required"
            ),
        ):
            codex_interface.confirm(plan, None, 10.0)

    def test_exact_yes_builds_only_stable_metadata_without_side_effects(
        self,
    ) -> None:
        plan = preview(self.ctx)
        approval = codex_interface.confirm(plan, lambda _: "yes", 10.0)
        bundle = plan_artifacts(plan, approval, "fixture-operation")
        self.assertEqual(approval.plan_digest, plan.digest)
        self.assertEqual(len(bundle.files), 4)
        self.assertNotIn(
            b"0.166.0", b"".join(item.raw for item in bundle.files)
        )
        self.assertEqual(bundle.policy_digest, plan.bridge.new_policy_digest)

    def test_run_requires_owned_operation_and_actual_yes_before_durable_intent(
        self,
    ) -> None:
        plan = preview(self.ctx)
        self.ctx.dry_run = False
        self.ctx.codex_interface = plan.digest
        self.ctx.interface_plan = plan
        rec = {"repo": str(self.repo), "sha": self.sha, "ts": self.ctx.ts}
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_approval_required"
        ):
            codex_interface.run(self.ctx, rec, plan)
        self.assertNotIn("codex_interface", rec)

    def test_normal_fake_upgrade_commits_rule_and_preserves_complete_old_queue(
        self,
    ) -> None:
        self.managed_release()
        request = self.request()
        plan = preview(self.ctx)
        self.ctx.dry_run = False
        self.ctx.codex_interface = plan.digest
        self.ctx.interface_plan = plan
        self.ctx.learning_prompt = lambda _: "yes"
        with mock.patch(
            "muninn.review_managed_files.matches", return_value=True
        ):
            result = installer.install(self.ctx, self.repo, self.sha)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT policy_digest FROM review_control"
                ).fetchone()[0],
                digest(plan.candidate),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT policy_digest,state FROM review_request WHERE id="
                    "?",
                    (request,),
                ).fetchone(),
                (digest(self.old), "pending"),
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_request").fetchone()[
                    0
                ],
                1,
            )
        self.assertEqual(result["codex_interface"]["phase"], "complete")

    def test_failed_upgrade_recovers_same_paused_grant(
        self,
    ) -> None:
        self.managed_release()
        self.request()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        self.world.fake.doctor = 1
        with self.assertRaises(StepFailedError):
            self.apply(plan)
        with closing(sqlite3.connect(self.path)) as conn:
            before = conn.execute(
                "SELECT policy_digest,admission,generation FROM review_contro"
                "l"
            ).fetchone()
        self.assertEqual(before[:2], (digest(plan.candidate), 0))
        self.assertIsNone(self.world.fake.loaded)
        self.world.fake.doctor = 0
        self.ctx.learning_prompt = lambda _: self.fail(
            "recovery requested another rule grant"
        )
        result = self.apply(plan)
        self.assertEqual(result["codex_interface"]["phase"], "complete")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT policy_digest,admission,generatio"
                    "n FROM review_contro"
                    "l"
                ).fetchone(),
                before,
            )

    def test_commit_pending_resume_recognizes_commit_without_replay(
        self,
    ) -> None:
        self.managed_release()
        self.request()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        original = codex_interface.phase

        def crash(
            ctx: Ctx, rec: Record, name: str, **changes: object
        ) -> codex_interface.InterfaceReceipt:
            if name == "grant_committed":
                raise KeyboardInterrupt(
                    "synthetic commit receipt interruption"
                )
            return original(ctx, rec, name, **changes)

        with (
            mock.patch.object(codex_interface, "phase", side_effect=crash),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.apply(plan)
        with closing(sqlite3.connect(self.path)) as conn:
            generation = conn.execute(
                "SELECT generation FROM review_control"
            ).fetchone()[0]
        self.ctx.learning_prompt = lambda _: self.fail(
            "commit recovery regranted authority"
        )
        result = self.apply(plan)
        self.assertEqual(result["codex_interface"]["phase"], "complete")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                generation,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_request").fetchone()[
                    0
                ],
                1,
            )

    def test_normal_explicit_restart_reuses_exact_managed_grant(self) -> None:
        self.managed_release()
        request = self.request()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        self.apply(plan)
        self.sql("UPDATE review_control SET admission=0")
        self.sql("INSERT INTO learning_setup VALUES(1,'now',NULL,1.0,NULL)")
        ctx = replace(
            self.world.ctx(upgrade=True, enable_learning=True),
            ts="managed-restart",
        )
        learning_scope.choose(ctx, None, lambda _: "yes")
        frozen = review_policy.capture(ctx, self.sha)
        self.assertEqual(frozen.get("digest"), digest(plan.candidate))
        result = installer.install(ctx, self.repo, self.sha)
        self.assertNotIn("codex_interface", result)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT policy_digest,admission FROM review_control"
                ).fetchone(),
                (digest(plan.candidate), 1),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT policy_digest,state FROM review_r"
                    "equest WHERE id=?",
                    (request,),
                ).fetchone(),
                (digest(self.old), "pending"),
            )
