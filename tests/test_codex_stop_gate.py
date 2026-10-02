"""Codex completion follows the full gate without recursively running tests."""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tools import codex_stop_gate


class CodexStopGateTest(unittest.TestCase):
    """A failed, unavailable or timed-out gate never permits completion."""

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
        with (
            mock.patch.object(codex_stop_gate.subprocess, "run") as run,
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
                [sys.executable, "-m", "tools.check", "--full"],
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
