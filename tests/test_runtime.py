"""Runtime ownership and source-root safety tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import runtime

ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "context.py"


class RuntimeRequestTest(unittest.TestCase):
    """Verify the shared bounded UDS recall recovery policy."""

    def test_recall_retries_after_healthy_status(self) -> None:
        """Recover one no-op scan fence without retrying blindly."""
        fenced = {
            "available": False,
            "evidence": [],
            "bytes": 0,
            "untrusted": True,
            "unavailable_reason": "reconciling",
        }
        recovered = {"available": True, "evidence": [{"text": "needle"}]}
        with patch(
            "hook_core._request_once",
            side_effect=[fenced, {"available": True}, recovered],
        ) as request_once:
            packet = runtime.request(
                Path("/private/tmp/brain.sock"),
                {"op": "recall", "prompt": "needle"},
                0.2,
                8_192,
            )
        self.assertEqual(packet, recovered)
        self.assertEqual(request_once.call_count, 3)

    def test_recall_does_not_retry_nonreconciling_reasons(self) -> None:
        """Leave unhealthy services fail-closed even if health changes."""
        for reason in (
            "rebuilding",
            "pending",
            "error",
            "root_unavailable",
            "unavailable",
        ):
            fenced = {
                "available": False,
                "evidence": [],
                "bytes": 0,
                "untrusted": True,
                "unavailable_reason": reason,
            }
            with (
                self.subTest(reason=reason),
                patch(
                    "hook_core._request_once",
                    side_effect=[fenced, {"available": True}],
                ) as request_once,
            ):
                packet = runtime.request(
                    Path("/private/tmp/brain.sock"),
                    {"op": "recall", "prompt": "needle"},
                    0.2,
                    8_192,
                )
            self.assertEqual(packet, fenced)
            self.assertEqual(request_once.call_count, 1)

    def test_transport_failure_does_not_retry(self) -> None:
        """Fail closed when the first UDS request cannot connect."""
        with patch(
            "hook_core._request_once", return_value=None
        ) as request_once:
            packet = runtime.request(
                Path("/private/tmp/brain.sock"),
                {"op": "recall", "prompt": "needle"},
                0.2,
                8_192,
            )
        self.assertFalse(packet["available"])
        self.assertEqual(request_once.call_count, 1)


class RuntimeTest(unittest.TestCase):
    """Exercise daemon exclusivity and fail-closed root state."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.base = base
        self.codex, self.claude = base / "codex", base / "claude"
        self.codex.mkdir()
        self.claude.mkdir()
        self.state, self.socket = base / "state", base / "brain.sock"
        self.database = base / "context.sqlite"
        self.process = self.start()

    def tearDown(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        self.temporary.cleanup()

    def start(self) -> subprocess.Popen[str]:
        """Start a private service and wait for its socket."""
        process = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "serve",
                "--codex-root",
                str(self.codex),
                "--claude-root",
                str(self.claude),
                "--db",
                str(self.database),
                "--state-dir",
                str(self.state),
                "--socket",
                str(self.socket),
                "--interval",
                "0.05",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(100):
            if self.socket.exists():
                return process
            time.sleep(0.02)
        raise AssertionError("daemon did not start")

    def command(self, name: str) -> subprocess.CompletedProcess[str]:
        """Call a public status command."""
        return subprocess.run(
            [sys.executable, str(CLI), name, "--socket", str(self.socket)],
            capture_output=True,
            text=True,
            check=False,
        )

    def wait_for_doctor(self, expected: int) -> None:
        """Wait for the daemon to leave a bounded reconcile window."""
        until = time.monotonic() + 2
        while time.monotonic() < until:
            if self.command("doctor").returncode == expected:
                return
            time.sleep(0.02)
        self.fail(f"doctor did not reach exit status {expected}")

    def test_unavailable_root_withholds_recall_until_recovery(self) -> None:
        """A missing configured root degrades doctor without losing history."""
        (self.codex / "one.jsonl").write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "root-needle",
                    }
                }
            )
            + "\n"
        )
        time.sleep(0.15)
        self.claude.rmdir()
        for _ in range(100):
            if self.command("doctor").returncode:
                break
            time.sleep(0.02)
        self.assertNotEqual(self.command("doctor").returncode, 0)
        self.claude.mkdir()
        for _ in range(100):
            if self.command("doctor").returncode == 0:
                return
            time.sleep(0.02)
        self.fail("doctor did not recover")

    def test_competing_daemon_cannot_replace_live_socket(self) -> None:
        """A second process fails before it can take over the socket path."""
        for _ in range(5):
            contender = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "serve",
                    "--codex-root",
                    str(self.codex),
                    "--claude-root",
                    str(self.claude),
                    "--db",
                    str(self.database),
                    "--state-dir",
                    str(self.state),
                    "--socket",
                    str(self.socket),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(contender.returncode, 0)
            self.assertTrue(self.socket.exists())
            self.wait_for_doctor(0)
        os.chmod(self.base, 0o755)
        self.wait_for_doctor(1)
        os.chmod(self.base, 0o700)
        self.wait_for_doctor(0)

    def test_sigkill_leaves_a_stale_socket_that_can_restart(self) -> None:
        """A killed owner leaves a stale socket that startup removes."""
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        self.assertTrue(self.socket.exists())
        self.process = self.start()
        self.assertEqual(self.command("status").returncode, 0)


if __name__ == "__main__":
    unittest.main()
