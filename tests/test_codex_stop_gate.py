"""Codex completion follows the full gate without recursively running tests."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import codex_stop_gate


class CodexStopGateTest(unittest.TestCase):
    """A failed, unavailable or timed-out gate never permits completion."""

    def test_selects_project_venv_instead_of_system_python(self) -> None:
        """The validation closure belongs to the checkout, not the launcher."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ".venv" / "bin" / "python3.13"
            python.parent.mkdir(parents=True)
            python.write_text("interpreter")
            (root / ".venv" / "pyvenv.cfg").write_text("home = /usr/local/bin")
            with (
                mock.patch.object(codex_stop_gate, "ROOT", root),
                mock.patch.object(sys, "platform", "darwin"),
            ):
                self.assertEqual(codex_stop_gate.gate_python(), python)

    def test_missing_project_venv_refuses(self) -> None:
        """A missing pinned runtime must not silently use global packages."""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(codex_stop_gate, "ROOT", Path(directory)),
            self.assertRaises(FileNotFoundError),
        ):
            codex_stop_gate.gate_python()

    def test_interpreter_without_venv_config_refuses(self) -> None:
        """An interpreter file alone can fall back to system packages."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ".venv" / "bin" / "python3.13"
            python.parent.mkdir(parents=True)
            python.write_text("interpreter")
            with (
                mock.patch.object(codex_stop_gate, "ROOT", root),
                mock.patch.object(sys, "platform", "darwin"),
                self.assertRaises(FileNotFoundError),
            ):
                codex_stop_gate.gate_python()

    @unittest.skipIf(
        os.name == "nt", "Windows symlink creation may need privilege"
    )
    def test_linked_bin_from_another_checkout_refuses(self) -> None:
        """A linked bin directory must not select another checkout's venv."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            foreign = root / "foreign" / "bin"
            foreign.mkdir(parents=True)
            (foreign / "python3.13").write_text("interpreter")
            venv = root / ".venv"
            venv.mkdir()
            (venv / "bin").symlink_to(foreign, target_is_directory=True)
            (venv / "pyvenv.cfg").write_text("home = /usr/local/bin")
            with (
                mock.patch.object(codex_stop_gate, "ROOT", root),
                mock.patch.object(sys, "platform", "darwin"),
                self.assertRaises(FileNotFoundError),
            ):
                codex_stop_gate.gate_python()

    def test_missing_venv_never_launches_global_gate(self) -> None:
        """A missing checkout runtime must fail before any gate launch."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(codex_stop_gate, "ROOT", Path(directory)),
            mock.patch.object(codex_stop_gate.subprocess, "run") as run,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(codex_stop_gate.main(), 2)
            run.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("pinned_validation_runtime_missing", stderr.getvalue())

    def test_selects_windows_project_venv(self) -> None:
        """Windows uses the checkout's own Scripts interpreter."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ".venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_text("interpreter")
            (root / ".venv" / "pyvenv.cfg").write_text("home = C:\\Python313")
            with (
                mock.patch.object(codex_stop_gate, "ROOT", root),
                mock.patch.object(sys, "platform", "win32"),
            ):
                self.assertEqual(codex_stop_gate.gate_python(), python)

    def run_gate(
        self,
        outcome: subprocess.CompletedProcess[str] | Exception,
        payload: str = "{}",
    ) -> tuple[int, str, str]:
        """Run the wrapper with a fake gate and collect its protocol output.

        Args:
            outcome: Completed subprocess or exception from launching it.
            payload: Codex hook input presented on stdin.

        Returns:
            Exit code, stdout and stderr from the wrapper.
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        python = Path("/pinned/python")
        with (
            mock.patch.object(codex_stop_gate.subprocess, "run") as run,
            mock.patch.object(
                codex_stop_gate, "gate_python", return_value=python
            ),
            mock.patch.object(sys, "stdin", io.StringIO(payload)),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            if isinstance(outcome, Exception):
                run.side_effect = outcome
            else:
                run.return_value = outcome
            code = codex_stop_gate.main()
            run.assert_called_once_with(
                [str(python), "-m", "tools.check", "--full"],
                cwd=codex_stop_gate.ROOT,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=540,
                check=False,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_success_returns_only_compact_json(self) -> None:
        result = subprocess.CompletedProcess([], 0, "ok tests\n", "")
        self.assertEqual(self.run_gate(result), (0, "{}\n", ""))

    def test_failure_blocks_with_gate_output(self) -> None:
        result = subprocess.CompletedProcess([], 1, "FAIL tests\n", "detail\n")
        code, stdout, stderr = self.run_gate(result)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("FAIL tests\ndetail\n", stderr)
        self.assertIn("fix the failures", stderr)

    def test_launch_error_blocks(self) -> None:
        code, stdout, stderr = self.run_gate(OSError("launch denied"))
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("launch denied", stderr)

    def test_timeout_blocks(self) -> None:
        code, stdout, stderr = self.run_gate(
            subprocess.TimeoutExpired("gate", 540)
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("timed out", stderr)

    def test_active_stop_never_bypasses_failed_gate(self) -> None:
        result = subprocess.CompletedProcess([], 1, "FAIL tests\n", "")
        for active in (False, True):
            with self.subTest(stop_hook_active=active):
                payload = (
                    '{"hook_event_name":"Stop","stop_hook_active":'
                    f'{str(active).lower()},"cwd":"/untrusted"}}'
                )
                code, stdout, stderr = self.run_gate(result, payload)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("FAIL tests", stderr)


if __name__ == "__main__":
    unittest.main()
