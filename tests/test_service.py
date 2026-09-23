"""End-to-end tests for continuous dual-corpus provenance context."""

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
from typing import cast

ROOT = Path(__file__).parents[1]
CONTEXT = ROOT / "scripts" / "context.py"
CODEX_HOOK = ROOT / "hooks" / "codex.py"


def run(
    *arguments: str, socket_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a public command with an optional service socket environment."""
    environment = os.environ | (
        {"PROVENANCE_CONTEXT_SOCKET": str(socket_path)} if socket_path else {}
    )
    return subprocess.run(
        [sys.executable, str(CONTEXT), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


class ServiceTest(unittest.TestCase):
    """Exercise append freshness, recovery, and client safety over UDS."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.codex = base / "codex"
        self.claude = base / "claude"
        self.codex.mkdir()
        self.claude.mkdir()
        self.database = base / "context.sqlite"
        self.state = base / "state"
        self.socket = base / "brain.sock"
        self.process = self.start()

    def tearDown(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        self.temporary.cleanup()

    def start(self) -> subprocess.Popen[str]:
        """Start the documented daemon command and wait for its socket."""
        process = subprocess.Popen(
            [
                sys.executable,
                str(CONTEXT),
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
            if process.poll() is not None:
                error = process.stderr
                raise AssertionError(
                    error.read() if error else "daemon failed"
                )
            time.sleep(0.02)
        process.kill()
        raise AssertionError("daemon did not create its socket")

    def recall(self, prompt: str) -> dict[str, object]:
        """Recall through the documented client surface."""
        result = run(
            "recall",
            "--socket",
            str(self.socket),
            "--prompt",
            prompt,
            "--repo",
            "/repo",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def wait_for(self, prompt: str) -> dict[str, object]:
        """Poll recall until the appended source appears."""
        until = time.monotonic() + 5
        while time.monotonic() < until:
            packet = self.recall(prompt)
            if packet["evidence"]:
                return packet
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {prompt}")

    def test_dual_append_recall_is_cited_and_sanitized(self) -> None:
        """Index complete Codex and Claude records without manual sync."""
        (self.codex / "one.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": "2026-09-23T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "codex-needle ready",
                        "cwd": "/repo",
                    },
                }
            )
            + "\n"
        )
        (self.claude / "two.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-23T00:00:01Z",
                    "cwd": "/repo",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "claude-needle ready"},
                            {"type": "thinking", "thinking": "hidden"},
                            {"type": "tool_use", "name": "Bash"},
                        ],
                    },
                }
            )
            + "\n"
        )
        codex = self.wait_for("codex-needle")
        claude = self.wait_for("claude-needle")
        codex_evidence = codex["evidence"]
        claude_evidence = claude["evidence"]
        self.assertIsInstance(codex_evidence, list)
        self.assertIsInstance(claude_evidence, list)
        codex_items = cast(list[object], codex_evidence)
        claude_items = cast(list[object], claude_evidence)
        codex_item = cast(dict[str, object], codex_items[0])
        claude_item = cast(dict[str, object], claude_items[0])
        codex_source = cast(dict[str, object], codex_item["source"])
        claude_source = cast(dict[str, object], claude_item["source"])
        self.assertEqual(codex_source["provider"], "codex")
        self.assertEqual(claude_source["provider"], "claude")
        self.assertNotIn("hidden", json.dumps(claude))
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)

    def test_restart_partial_tail_and_unavailable_client_are_safe(
        self,
    ) -> None:
        """Reconcile missed writes and retry partial data safely."""
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        source = self.codex / "recovery.jsonl"
        complete = {
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "restart-needle ready",
                "cwd": "/repo",
            }
        }
        source.write_text(json.dumps(complete) + "\n")
        self.process = self.start()
        self.wait_for("restart-needle")
        partial = json.dumps(
            {
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "tail-needle ready",
                    "cwd": "/repo",
                }
            }
        )
        with source.open("a") as stream:
            stream.write(partial[:-1])
        time.sleep(0.15)
        status = run("status", "--socket", str(self.socket))
        self.assertIn("pending_sources", status.stdout)
        self.assertNotIn("tail-needle", json.dumps(self.recall("tail-needle")))
        with source.open("a") as stream:
            stream.write(partial[-1:] + "\n")
        self.wait_for("tail-needle")
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=5)
        unavailable = run(
            "recall",
            "--socket",
            str(self.socket),
            "--prompt",
            "restart-needle",
        )
        self.assertEqual(unavailable.returncode, 0)
        self.assertFalse(json.loads(unavailable.stdout)["available"])
        hook = subprocess.run(
            [sys.executable, str(CODEX_HOOK)],
            input=json.dumps(
                {"hook_event_name": "UserPromptSubmit", "prompt": "needle"}
            ),
            env=os.environ | {"PROVENANCE_CONTEXT_SOCKET": str(self.socket)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(json.loads(hook.stdout), {})


if __name__ == "__main__":
    unittest.main()
