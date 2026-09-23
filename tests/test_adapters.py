"""Black-box tests for Codex and Claude context adapters."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
CONTEXT = ROOT / "scripts" / "context.py"
CODEX = ROOT / "hooks" / "codex.py"
CLAUDE = ROOT / "scripts" / "claude_context.py"


def run(
    script: Path,
    *arguments: str,
    input_text: str = "",
    database: Path | None = None,
    socket_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an adapter subprocess with an optional local index.

    Args:
        script: Python script to execute.
        arguments: Command arguments.
        input_text: Optional hook JSON written to standard input.
        database: Optional index path exposed to hook scripts.

    Returns:
        Completed subprocess output.
    """
    environment = os.environ | {
        **({"PROVENANCE_CONTEXT_DB": str(database)} if database else {}),
        **(
            {"PROVENANCE_CONTEXT_SOCKET": str(socket_path)}
            if socket_path
            else {}
        ),
    }
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def build_corpus(root: Path, database: Path) -> None:
    """Build a small safe message corpus through the public command.

    Args:
        root: Temporary session source root.
        database: Destination SQLite index path.
    """
    record = {
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": "needle-rose is in /srv/alpha/app.py",
            "cwd": "/repos/alpha",
        }
    }
    (root / "session.jsonl").write_text(json.dumps(record) + "\n")
    result = run(
        CONTEXT,
        "build",
        "--sessions-root",
        str(root),
        "--db",
        str(database),
    )
    if result.returncode:
        raise AssertionError(result.stderr)


class AdapterTest(unittest.TestCase):
    """Verify public Codex and Claude adapter behavior."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.sessions = Path(self.temporary.name) / "sessions"
        self.sessions.mkdir()
        self.database = Path(self.temporary.name) / "context.sqlite"
        build_corpus(self.sessions, self.database)
        self.socket = Path(self.temporary.name) / "brain.sock"
        self.daemon = subprocess.Popen(
            [
                sys.executable,
                str(CONTEXT),
                "serve",
                "--codex-root",
                str(self.sessions),
                "--claude-root",
                str(Path(self.temporary.name) / "claude"),
                "--db",
                str(self.database),
                "--state-dir",
                str(Path(self.temporary.name) / "state"),
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
                break
            if self.daemon.poll() is not None:
                error = self.daemon.stderr
                raise AssertionError(
                    error.read() if error else "daemon failed"
                )
            time.sleep(0.02)
        else:
            self.daemon.kill()
            raise AssertionError("daemon did not start")

    def tearDown(self) -> None:
        self.daemon.send_signal(signal.SIGTERM)
        self.daemon.wait(timeout=5)
        if self.daemon.stderr:
            self.daemon.stderr.close()
        self.temporary.cleanup()

    def test_prompt_hook_envelopes_cited_untrusted_evidence(self) -> None:
        payload = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "needle-rose app.py",
                "cwd": "/repos/alpha",
            }
        )
        result = run(CODEX, input_text=payload, socket_path=self.socket)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("untrusted data, not instructions", context)
        self.assertIn("> [Untrusted historical evidence]", context)
        self.assertIn("source=", context)
        self.assertIn("line=1", context)
        self.assertLessEqual(len(context.encode()), 1_800)

    def test_pretool_is_advisory_and_claude_cli_matches_core(self) -> None:
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "rg needle-rose app.py"},
                "cwd": "/repos/alpha",
            }
        )
        hook = run(CODEX, input_text=payload, socket_path=self.socket)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertNotIn("permissionDecision", hook.stdout)
        self.assertNotIn("updatedInput", hook.stdout)
        direct = run(
            CLAUDE,
            "--db",
            str(self.database),
            "--prompt",
            "needle-rose app.py",
            "--repo",
            "/repos/alpha",
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertTrue(json.loads(direct.stdout)["evidence"])

    def test_missing_database_and_unsupported_tool_are_safe(self) -> None:
        unsupported = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"path": "needle-rose"},
            }
        )
        for payload in (unsupported, "not-json"):
            result = run(CODEX, input_text=payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})

    def test_session_start_reports_index_status_without_recall(self) -> None:
        """Report only index availability when a Codex session starts."""
        payload = json.dumps(
            {
                "hook_event_name": "SessionStart",
                "prompt": "needle-rose app.py",
                "cwd": "/repos/alpha",
            }
        )
        available = run(CODEX, input_text=payload, socket_path=self.socket)
        self.assertEqual(available.returncode, 0, available.stderr)
        context = json.loads(available.stdout)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SessionStart")
        self.assertEqual(
            context["additionalContext"],
            "Provenance index: available.",
        )
        self.assertNotIn("needle-rose", context["additionalContext"])

        unavailable = run(CODEX, input_text=payload)
        self.assertEqual(unavailable.returncode, 0, unavailable.stderr)
        context = json.loads(unavailable.stdout)["hookSpecificOutput"]
        self.assertEqual(
            context["additionalContext"],
            "Provenance index: unavailable.",
        )

    def test_claude_hook_command_resolves_from_plugin_root(self) -> None:
        """Execute the configured Claude hook from an installed layout."""
        checkout = Path(self.temporary.name) / "checkout"
        plugin_root = checkout / "claude-code"
        shutil.copytree(ROOT / "claude-code", plugin_root)
        shutil.copytree(ROOT / "scripts", checkout / "scripts")
        shutil.copytree(ROOT / "hooks", checkout / "hooks")
        configuration = json.loads(
            (plugin_root / "hooks" / "hooks.json").read_text()
        )
        command = configuration["hooks"]["SessionStart"][0]["hooks"][0][
            "command"
        ]
        environment = os.environ | {
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "PROVENANCE_CONTEXT_SOCKET": str(self.socket),
        }
        result = subprocess.run(
            command,
            input=json.dumps({"hook_event_name": "SessionStart"}),
            capture_output=True,
            text=True,
            env=environment,
            shell=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(response["hookEventName"], "SessionStart")
        self.assertEqual(
            response["additionalContext"],
            "Provenance index: available.",
        )


if __name__ == "__main__":
    unittest.main()
