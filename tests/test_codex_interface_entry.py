"""Check public interface routing without native setup or account access."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
from unittest import mock

from install import entry, installer
from install.context import StepFailedError
from tests.host_renewal_support import states
from tests.installer_support import ROOT, git
from tests.test_codex_interface_preview import InterfacePreviewTests


class InterfaceEntryTests(InterfacePreviewTests):
    """Keep public previews nonwriting through both supported entry paths."""

    def test_native_entry_forwards_exact_checkout_and_flags(self) -> None:
        args = ["--upgrade", "--codex-interface", "preview", "--check"]
        with (
            mock.patch.object(entry, "ROOT", self.repo),
            mock.patch.object(entry, "_head", return_value=self.sha),
            mock.patch.object(installer, "main", return_value=0) as invoke,
        ):
            self.assertEqual(entry.main(args), 0)
        self.assertEqual(
            invoke.call_args.args[0],
            ["--repo", str(self.repo), "--sha", self.sha, *args],
        )

    def test_public_preview_does_not_prompt_or_change_state(self) -> None:
        self.ctx.codex_interface = "preview"
        before = states(self.home)
        with (
            mock.patch.object(installer, "Ctx", return_value=self.ctx),
            mock.patch("builtins.input", side_effect=AssertionError("prompt")),
            mock.patch(
                "install.learning_scope.choose",
                side_effect=AssertionError("scope"),
            ),
        ):
            result = installer.main(
                [
                    "--repo",
                    str(self.repo),
                    "--sha",
                    self.sha,
                    "--upgrade",
                    "--codex-interface",
                    "preview",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(states(self.home), before)

    def test_public_failure_does_not_expose_private_exception_text(
        self,
    ) -> None:
        self.ctx.codex_interface = "preview"
        for error, code in (
            (
                OSError("private-account-path"),
                "codex_interface_recovery_required",
            ),
            (
                StepFailedError("runtime_probe_unavailable"),
                "runtime_probe_unavailable",
            ),
        ):
            output = io.StringIO()
            with (
                mock.patch.object(installer, "Ctx", return_value=self.ctx),
                mock.patch(
                    "install.codex_interface.preview", side_effect=error
                ),
                contextlib.redirect_stdout(output),
            ):
                result = installer.main(
                    [
                        "--repo",
                        str(self.repo),
                        "--sha",
                        self.sha,
                        "--upgrade",
                        "--codex-interface",
                        "preview",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue().strip(), "FAILED: " + code)

    def test_shell_preview_forwards_checkout_without_setup(self) -> None:
        script = self.repo / "bin/muninn-install"
        shutil.copyfile(ROOT / "bin/muninn-install", script)
        script.chmod(0o755)
        git(self.repo, "add", "bin/muninn-install")
        git(self.repo, "commit", "-qm", "synthetic wrapper")
        python = self.home.parent / "fake-python"
        python.write_text(
            '#!/bin/sh\ncase "$*" in\n*"print(sys.platf'
            'orm)"*) echo darwin ;;\n*"-m install.inst'
            'aller"*) printf "%s\\n" "$@" ;;\n*) exit 0'
            " ;;\nesac\n"
        )
        python.chmod(0o755)
        before = states(self.home)
        for flags in (
            ["--codex-interface", "preview"],
            ["--codex-interface=preview"],
        ):
            with self.subTest(flags=flags):
                result = subprocess.run(
                    [str(script), "--upgrade", *flags],
                    env=dict(
                        os.environ,
                        HOME=str(self.home),
                        MUNINN_PYTHON=str(python),
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
        self.assertEqual(states(self.home), before)
