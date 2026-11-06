"""Keep one approved physical enrollment through apply and recovery fences."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

from install import codex_interface, installer, record, steps_release
from install.codex_interface_preview import preview
from install.context import StepFailedError
from muninn.review_progress import digest
from tests.codex_interface_support import InterfaceApplyFixture
from tests.installer_support import git
from tests.managed_codex_support import physical_host
from tools.codex_policy_artifacts import PreparedFile


class InterfaceHostApplyTests(InterfaceApplyFixture):
    """Use temporary files and fake physical IDs without native execution."""

    def test_name_changes_do_not_change_the_approved_diagnostic(self) -> None:
        self.managed_release()
        self.executable.unlink()
        plan = preview(self.ctx)
        prompts: list[str] = []

        def consent(text: str) -> str:
            prompts.append(text)
            return "yes"

        self.ctx.learning_prompt = consent
        with mock.patch(
            "muninn.review_codex_policy.host_identity",
            side_effect=lambda: physical_host().digest,
        ):
            result = self.apply(plan)
        self.assertEqual(len(prompts), 1)
        self.assertIn("physical continuity", prompts[0])
        self.assertEqual(result["codex_interface"]["phase"], "complete")
        self.assertEqual(
            result["codex_interface"]["plan"]["host_enrollment"],
            plan.host_enrollment.model_dump(mode="json"),
        )

    def test_other_physical_host_after_yes_cannot_record_intent(self) -> None:
        self.managed_release()
        plan = preview(self.ctx)

        def consent(_: str) -> str:
            observed.return_value = physical_host("7" * 64)
            return "yes"

        self.ctx.learning_prompt = consent
        with (
            mock.patch(
                "muninn.review_managed_host.observe",
                return_value=self.physical,
            ) as observed,
            self.assertRaisesRegex(StepFailedError, "codex_host_changed"),
        ):
            self.apply(plan)
        retained = record.load_record(self.ctx.rdir / "rollback-record.json")
        self.assertNotIn("codex_interface", retained)

    def test_physical_change_after_publication_refuses_sql_commit(
        self,
    ) -> None:
        self.managed_release()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        publish = codex_interface.publish_files

        def mutate(home: Path, files: tuple[PreparedFile, ...]) -> None:
            publish(home, files)
            observed.return_value = physical_host("7" * 64)

        with (
            mock.patch(
                "muninn.review_managed_host.observe",
                return_value=self.physical,
            ) as observed,
            mock.patch.object(
                codex_interface, "publish_files", side_effect=mutate
            ),
            self.assertRaisesRegex(StepFailedError, "codex_host_changed"),
        ):
            self.apply(plan)
        self.assertEqual(self.policy_digest(), digest(self.old))

    def test_clean_old_parser_cannot_publish_a_new_grant(self) -> None:
        self.ctx.learning_prompt = lambda _: "yes"
        plan = preview(self.ctx)
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_runtime_required"
        ):
            self.apply(plan)
        self.assertEqual(self.policy_digest(), digest(self.old))

    def test_old_marker_alone_cannot_claim_host_parser_support(self) -> None:
        self.managed_release()
        marker = self.repo / "muninn/review_runtime_models.py"
        marker.write_text(
            '"""Old fixture marker."""\n'
            "from __future__ import annotations\n"
            'CONTRACT = "codex_managed_interface_v1"\n'
        )
        git(self.repo, "add", str(marker.relative_to(self.repo)))
        git(self.repo, "commit", "-qm", "fixture old marker")
        self.sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.ctx.learning_prompt = lambda _: "yes"
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_runtime_required"
        ):
            self.apply(preview(self.ctx))
        self.assertEqual(self.policy_digest(), digest(self.old))

    def test_runtime_swap_after_publication_refuses_sql_commit(self) -> None:
        self.managed_release()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        publish = codex_interface.publish_files

        def mutate(home: Path, files: tuple[PreparedFile, ...]) -> None:
            publish(home, files)
            source = self.ctx.lib / self.sha / "muninn/review_models.py"
            source.write_bytes(source.read_bytes() + b"\n# changed\n")

        with (
            mock.patch.object(
                codex_interface, "publish_files", side_effect=mutate
            ),
            self.assertRaisesRegex(
                StepFailedError, "codex_interface_runtime_required"
            ),
        ):
            self.apply(plan)
        self.assertEqual(self.policy_digest(), digest(self.old))

    def test_committed_resume_refuses_a_changed_runtime_without_reconsent(
        self,
    ) -> None:
        self.managed_release()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        self.world.fake.doctor = 1
        with self.assertRaises(StepFailedError):
            self.apply(plan)
        source = self.ctx.lib / self.sha / "muninn/review_codex_policy.py"
        source.write_bytes(source.read_bytes() + b"\n# changed\n")
        self.ctx.learning_prompt = lambda _: self.fail("another consent")
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_runtime_required"
        ):
            self.apply(plan)
        self.assertEqual(self.policy_digest(), digest(plan.candidate))
        self.assertIsNone(self.world.fake.loaded)

    def test_committed_resume_refuses_an_unavailable_physical_id(self) -> None:
        self.managed_release()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        self.world.fake.doctor = 1
        with self.assertRaises(StepFailedError):
            self.apply(plan)
        with (
            mock.patch(
                "muninn.review_managed_host.observe",
                side_effect=ValueError("codex_host_identity_unavailable"),
            ),
            self.assertRaisesRegex(
                StepFailedError, "codex_host_identity_unavailable"
            ),
        ):
            self.apply(plan)
        self.assertEqual(self.policy_digest(), digest(plan.candidate))
        self.assertIsNone(self.world.fake.loaded)

    def test_ordinary_managed_restart_checks_the_selected_parser(self) -> None:
        self.managed_release()
        self.ctx.learning_prompt = lambda _: "yes"
        result = self.apply(preview(self.ctx))
        result.pop("codex_interface")
        self.ctx.codex_interface = None
        self.ctx.interface_plan = None
        source = self.ctx.lib / self.sha / "muninn/review_runtime_models.py"
        source.write_bytes(source.read_bytes() + b"\n# changed\n")
        with (
            mock.patch(
                "install.steps_release.must",
                side_effect=AssertionError("unexpected service start"),
            ),
            self.assertRaisesRegex(
                StepFailedError, "codex_interface_runtime_required"
            ),
        ):
            steps_release.restart(self.ctx, result)

    def policy_digest(self) -> str:
        """Read the current policy without changing the store."""
        with closing(sqlite3.connect(self.path)) as conn:
            return str(
                conn.execute(
                    "SELECT policy_digest FROM review_control"
                ).fetchone()[0]
            )

    def test_enable_learning_keeps_the_exact_physical_refusal_code(
        self,
    ) -> None:
        self.managed_release()
        self.ctx.learning_prompt = lambda _: "yes"
        plan = preview(self.ctx)
        self.apply(plan)
        current = replace(
            self.ctx,
            ts="20261002T000000Z",
            codex_interface=None,
            interface_plan=None,
            interface_operation=None,
            enable_learning=True,
        )
        current.learning_prompt = lambda _: self.fail("unexpected new consent")
        for code in ("codex_host_identity_unavailable", "codex_host_changed"):
            with (
                self.subTest(code=code),
                mock.patch(
                    "muninn.review_managed_host.observe",
                    side_effect=ValueError(code),
                ),
                self.assertRaisesRegex(StepFailedError, "^" + code + "$"),
            ):
                installer.install(current, self.repo, self.sha)
            self.assertEqual(self.policy_digest(), digest(plan.candidate))
