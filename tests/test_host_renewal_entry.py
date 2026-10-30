"""Keep renewal previews nonwriting through the actual installer entry."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
from unittest import mock

from install import entry, host_renewal, installer, legacy_state, lifecycle
from install.context import Ctx, StepFailedError
from install.host_renewal_operation import (
    HeldOperation,
    held_operation,
    observe_operation,
    validate_operation,
)
from install.host_renewal_preview import preview
from tests.host_renewal_support import HostFixture, states
from tests.installer_support import ROOT, git
from tests.test_host_renewal_apply import ApplyFixture


class HostRenewalEntryTests(HostFixture):
    """Select preview before mutating setup."""

    def test_later_position_check_is_nonwriting(self) -> None:
        before = states(self.home)
        with (
            mock.patch.object(installer, "Ctx", return_value=self.ctx),
            mock.patch("builtins.input", side_effect=AssertionError("prompt")),
            mock.patch(
                "install.learning_scope.choose",
                side_effect=AssertionError("scope"),
            ),
        ):
            try:
                result = installer.main(
                    [
                        "--repo",
                        str(self.repo),
                        "--sha",
                        self.sha,
                        "--upgrade",
                        "--review-policy",
                        str(self.ctx.review_policy),
                        "--renew-codex-host",
                        "preview",
                        "--check",
                    ]
                )
            except SystemExit as error:
                result = error.code
        self.assertEqual(result, 0)
        self.assertEqual(states(self.home), before)

    def test_real_held_lock_projection_preserves_initial_digest(self) -> None:

        for mode in (0o600, 0o700):
            with self.subTest(mode=mode):
                lock = self.ctx.lib / ".install-record.lock"
                lock.write_bytes(b"retained lock")
                lock.chmod(mode)
                plan = preview(self.ctx, self.repo, self.sha)
                ctx = dataclasses.replace(self.ctx, dry_run=False)
                with held_operation(
                    ctx,
                    approved=plan.operation_lock,
                    expected_before=plan.operation_lock,
                ) as operation:
                    current = preview(
                        ctx, self.repo, self.sha, operation=operation
                    )
                    self.assertEqual(current.digest, plan.digest)
                    if mode == 0o600:
                        self.assertEqual(operation.held, plan.operation_lock)
                    self.assertEqual(
                        operation.held["stamp"][:2], list(operation.identity)
                    )

    def test_native_entry_supplies_checkout_for_renewal(self) -> None:
        with (
            mock.patch.object(entry, "ROOT", self.repo),
            mock.patch.object(entry, "_head", return_value=self.sha),
            mock.patch.object(installer, "main", return_value=0) as invoke,
        ):
            self.assertEqual(
                entry.main(
                    [
                        "--upgrade",
                        "--review-policy",
                        str(self.ctx.review_policy),
                        "--renew-codex-host",
                        "preview",
                        "--check",
                    ]
                ),
                0,
            )
        self.assertIn("--repo", invoke.call_args.args[0])
        self.assertIn(str(self.repo), invoke.call_args.args[0])
        self.assertIn(self.sha, invoke.call_args.args[0])

    def test_shell_later_check_does_not_setup_or_prompt(self) -> None:
        script = self.repo / "bin/muninn-install"
        shutil.copyfile(ROOT / "bin/muninn-install", script)
        script.chmod(0o755)
        git(self.repo, "add", "bin/muninn-install")
        git(self.repo, "commit", "-qm", "synthetic wrapper")
        python = self.home.parent / "fake-python"
        python.write_text(
            '#!/bin/sh\ncase "$*" in\n*"print(sys.platform)"*) echo '
            'darwin ;;\n*"-m install.installer"*) printf "%s\\n" "$@" '
            ";;\n*) exit 0 ;;\nesac\n"
        )
        python.chmod(0o755)
        before = states(self.home)
        result = subprocess.run(
            [
                str(script),
                "--upgrade",
                "--review-policy",
                str(self.ctx.review_policy),
                "--renew-codex-host",
                "preview",
                "--check",
            ],
            env=dict(
                os.environ, HOME=str(self.home), MUNINN_PYTHON=str(python)
            ),
            input="",
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Enable this checkout", result.stderr)
        self.assertIn("--repo", result.stdout)
        self.assertIn("--check", result.stdout)
        self.assertEqual(states(self.home), before)

    def test_fake_expired_nested_and_absent_operation_proofs_refuse(
        self,
    ) -> None:
        ctx = dataclasses.replace(self.ctx, dry_run=False)
        with (
            self.assertRaisesRegex(
                StepFailedError, "host_renewal_state_changed"
            ),
            held_operation(ctx, approved=None, expected_before=None),
        ):
            self.fail("absence was treated as exclusive creation")
        path = ctx.lib / ".install-record.lock"
        self.assertFalse(path.exists())
        path.write_bytes(b"")
        path.chmod(0o600)
        before = observe_operation(ctx)
        with mock.patch.object(
            legacy_state, "operation", wraps=legacy_state.operation
        ) as acquire:
            with held_operation(
                ctx, approved=before, expected_before=before
            ) as actual:
                with self.assertRaisesRegex(
                    StepFailedError, "host_renewal_approval_required"
                ):
                    validate_operation(ctx, dataclasses.replace(actual))
                with (
                    self.assertRaisesRegex(
                        StepFailedError, "host_renewal_state_changed"
                    ),
                    held_operation(
                        ctx, approved=before, expected_before=before
                    ),
                ):
                    self.fail("nested ownership")
            self.assertEqual(acquire.call_count, 1)
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_approval_required"
        ):
            validate_operation(ctx, actual)
        fake = HeldOperation(
            before,
            before,
            actual.held,
            actual.identity,
            lambda: actual.identity,
        )
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_approval_required"
        ):
            validate_operation(ctx, fake)

    def test_only_explicit_upgrade_policy_route_accepts_syntax(self) -> None:
        base = [
            "--repo",
            str(self.repo),
            "--sha",
            self.sha,
            "--upgrade",
            "--review-policy",
            str(self.ctx.review_policy),
            "--renew-codex-host",
            "preview",
            "--check",
        ]
        for change in ("fresh", "enable", "scope", "policy"):
            args = list(base)
            if change == "fresh":
                args[args.index("--upgrade")] = "--fresh"
            elif change == "enable":
                args.append("--enable-learning")
            elif change == "scope":
                args += ["--learning-scope", "all"]
            else:
                index = args.index("--review-policy")
                del args[index : index + 2]
            before = states(self.home)
            with (
                self.subTest(change=change),
                mock.patch.object(installer, "Ctx", return_value=self.ctx),
                mock.patch(
                    "builtins.input", side_effect=AssertionError("prompt")
                ),
            ):
                self.assertEqual(installer.main(args), 1)
            self.assertEqual(states(self.home), before)

    def test_no_and_noninteractive_approval_refuse_before_disruption(
        self,
    ) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        args = [
            "--repo",
            str(self.repo),
            "--sha",
            self.sha,
            "--upgrade",
            "--review-policy",
            str(self.ctx.review_policy),
            "--renew-codex-host",
            plan.digest,
        ]
        for interactive, answer in ((True, "no"), (False, "yes")):
            before = states(self.home)
            ctx = dataclasses.replace(self.ctx, dry_run=False)
            with (
                self.subTest(interactive=interactive),
                mock.patch.object(installer, "Ctx", return_value=ctx),
                mock.patch("sys.stdin.isatty", return_value=interactive),
                mock.patch("builtins.input", return_value=answer),
                mock.patch.object(
                    installer,
                    "install",
                    side_effect=AssertionError("disrupted"),
                ),
            ):
                self.assertEqual(installer.main(args), 1)
            self.assertEqual(states(self.home), before)

    def test_native_first_and_later_check_use_actual_readonly_entry(
        self,
    ) -> None:
        for position in (0, 3):
            args = [
                "--upgrade",
                "--review-policy",
                str(self.ctx.review_policy),
                "--renew-codex-host",
                "preview",
            ]
            args.insert(position, "--check")
            before = states(self.home)
            with (
                mock.patch.object(entry, "ROOT", self.repo),
                mock.patch.object(entry, "_head", return_value=self.sha),
                mock.patch.object(installer, "Ctx", return_value=self.ctx),
                mock.patch(
                    "builtins.input", side_effect=AssertionError("prompt")
                ),
            ):
                self.assertEqual(entry.main(args), 0)
            self.assertEqual(states(self.home), before)

    def test_exception_cleanup_expires_proof_and_substitution_refuses(
        self,
    ) -> None:
        ctx = dataclasses.replace(self.ctx, dry_run=False)
        path = ctx.lib / ".install-record.lock"
        path.write_bytes(b"lock")
        path.chmod(0o600)
        before = observe_operation(ctx)
        with (
            self.assertRaisesRegex(RuntimeError, "synthetic"),
            held_operation(
                ctx, approved=before, expected_before=before
            ) as failed,
        ):
            raise RuntimeError("synthetic interruption")
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_approval_required"
        ):
            validate_operation(ctx, failed)
        with held_operation(
            ctx, approved=before, expected_before=before
        ) as current:
            validate_operation(ctx, current)
        with (
            self.assertRaises(StepFailedError),
            held_operation(
                ctx, approved=before, expected_before=before
            ) as replaced,
        ):
            path.rename(path.with_suffix(".retained"))
            path.write_bytes(b"lock")
            path.chmod(0o600)
            validate_operation(ctx, replaced)
        with (
            self.assertRaisesRegex(
                StepFailedError, "host_renewal_state_changed"
            ),
            held_operation(ctx, approved=before, expected_before=before),
        ):
            self.fail("substitution admitted")


class HostRenewalDiscoveryTests(ApplyFixture):
    """Keep damaged-history refusals ahead of any new installer effects."""

    def assert_refusal(self, ctx: Ctx, code: str) -> None:
        """Require public installer refusal without file or policy effects."""
        before, control = states(self.home), self.read_control()
        with (
            mock.patch("builtins.input", side_effect=AssertionError("prompt")),
            mock.patch.object(
                lifecycle, "stop", side_effect=AssertionError("stop")
            ),
            mock.patch.object(
                host_renewal, "activate", side_effect=AssertionError("grant")
            ),
            self.assertRaisesRegex(StepFailedError, code),
        ):
            installer.install(ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)
        self.assertEqual(self.read_control(), control)

    def test_explicit_digest_missing_record_keeps_host_refusal(self) -> None:
        plan = self.interrupt_at("contained_archived")
        (self.ctx.rdir / "rollback-record.json").unlink()
        ctx = dataclasses.replace(
            self.ctx, dry_run=False, renew_codex_host=plan.digest
        )
        self.assert_refusal(ctx, "host_renewal_record_changed")

    def test_ordinary_entry_refuses_pending_or_damaged_host_history(
        self,
    ) -> None:
        for state, code in (
            ("pending", "host_renewal_recovery_required"),
            ("malformed", "host_renewal_record_invalid"),
            ("missing", "host_renewal_record_changed"),
        ):
            with self.subTest(state=state):
                self.doCleanups()
                self.setUp()
                self.world.fake.doctor = 1
                plan = preview(self.ctx, self.repo, self.sha)
                with self.assertRaisesRegex(
                    AssertionError, "actual renewal entry refused"
                ):
                    self.run_renewal(plan)
                if state == "malformed":
                    path = self.ctx.rdir / "rollback-record.json"
                    value = json.loads(path.read_bytes())
                    value["host_renewal"]["version"] = "invalid"
                    path.write_text(json.dumps(value))
                elif state == "missing":
                    self.ctx.rdir.rename(
                        self.ctx.rdir.with_name("retained-host-record")
                    )
                ctx = dataclasses.replace(self.ctx, dry_run=False)
                self.assert_refusal(ctx, code)
                if state != "pending":
                    self.assert_refusal(
                        dataclasses.replace(ctx, renew_codex_host=plan.digest),
                        code,
                    )
