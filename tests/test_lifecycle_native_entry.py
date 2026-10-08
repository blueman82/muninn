"""Native source entry checks preserve isolated state and failure evidence."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install.context import Ctx, run_real
from tests import lifecycle_native_entry


class NativeEntryTests(unittest.TestCase):
    """Exercise real filesystem fingerprints around bounded wrapper calls."""

    def test_fingerprint_detects_same_size_content_and_mode_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            file = home / "state"
            file.write_bytes(b"old")
            before = lifecycle_native_entry.fingerprint(home)
            file.write_bytes(b"new")
            self.assertNotEqual(
                before, lifecycle_native_entry.fingerprint(home)
            )
            before = lifecycle_native_entry.fingerprint(home)
            file.chmod(0o400)
            self.assertNotEqual(
                before, lifecycle_native_entry.fingerprint(home)
            )

    def test_readonly_refuses_actual_home_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            ctx = Ctx(home, run_real, "entry", platform="linux")

            def mutate(
                *args: object, **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                (home / "unexpected").write_bytes(b"effect")
                return subprocess.CompletedProcess([], 0, b"", b"")

            with (
                patch.object(lifecycle_native_entry.sys, "platform", "linux"),
                patch.object(
                    lifecycle_native_entry, "run_wrapper", side_effect=mutate
                ),
                self.assertRaisesRegex(
                    AssertionError, "readonly wrapper changed home"
                ),
            ):
                lifecycle_native_entry.readonly(
                    ctx, home, "muninn-install", "--check"
                )

    def test_source_environment_isolated_from_real_provider_overrides(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(Path(temporary), run_real, "entry", platform="linux")
            with patch.dict(
                "os.environ",
                {"CODEX_HOME": "/foreign", "CLAUDE_CONFIG_DIR": "/foreign"},
            ):
                env = lifecycle_native_entry.environment(ctx)
            self.assertEqual(env["HOME"], str(ctx.home))
            self.assertEqual(env["USERPROFILE"], str(ctx.home))
            self.assertEqual(env["CODEX_HOME"], str(ctx.codex_home))
            self.assertEqual(
                env["CLAUDE_CONFIG_DIR"], str(ctx.settings.parent)
            )
            self.assertEqual(env["MUNINN_HOME"], str(ctx.data))

    def test_native_entry_refuses_foreign_platform_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            ctx = Ctx(home, run_real, "entry", platform="linux")
            with (
                patch.object(lifecycle_native_entry.sys, "platform", "darwin"),
                patch.object(lifecycle_native_entry.subprocess, "run") as run,
                self.assertRaisesRegex(OSError, "native_linux_required"),
            ):
                lifecycle_native_entry.run_wrapper(ctx, home, "muninn-install")
            run.assert_not_called()

    def test_status_requires_selected_sha_and_current_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(Path(temporary), run_real, "entry", platform="linux")
            with (
                patch.object(
                    lifecycle_native_entry,
                    "readonly",
                    return_value=(
                        b"installed abcdef0, "
                        b"checkout abcdef0: up to date\n"
                    ),
                ) as readonly,
            ):
                lifecycle_native_entry.status(
                    ctx, ctx.home, "abcdef0" + "0" * 33
                )
            readonly.assert_called_once_with(
                ctx, ctx.home, "muninn-install", "--status"
            )
            with (
                patch.object(
                    lifecycle_native_entry,
                    "readonly",
                    return_value=b"not installed",
                ),
                self.assertRaisesRegex(
                    AssertionError, "source status differs"
                ),
            ):
                lifecycle_native_entry.status(
                    ctx, ctx.home, "abcdef0" + "0" * 33
                )
