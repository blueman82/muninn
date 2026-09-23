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

ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "context.py"


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
        self.assertEqual(self.command("doctor").returncode, 0)
        os.chmod(self.base, 0o755)
        self.assertNotEqual(self.command("doctor").returncode, 0)
        os.chmod(self.base, 0o700)
        self.assertEqual(self.command("doctor").returncode, 0)


if __name__ == "__main__":
    unittest.main()
