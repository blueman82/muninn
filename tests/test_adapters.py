"""Black-box tests for Codex and Claude context adapters."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from importlib import import_module
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
CONTEXT = ROOT / "scripts" / "context.py"
CODEX = ROOT / "hooks" / "codex.py"
CLAUDE = ROOT / "scripts" / "claude_context.py"
LAUNCHD = ROOT / "launchd" / "com.provenance-context.plist.template"
sys.path.insert(0, str(ROOT / "claude-code" / "scripts"))
codex = import_module("hook_core")


def run(
    script: Path,
    *arguments: str,
    input_text: str = "",
    database: Path | None = None,
    socket_path: Path | None = None,
    home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an adapter subprocess with an optional local index.

    Args:
        script: Python script to execute.
        arguments: Command arguments.
        input_text: Optional hook JSON written to standard input.
        database: Optional index path exposed to hook scripts.
        socket_path: Optional private socket exposed to hook scripts.
        home: Optional current-user home for default socket resolution.

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
        **({"HOME": str(home)} if home else {}),
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
        self.claude_sessions = Path(self.temporary.name) / "claude"
        self.claude_sessions.mkdir()
        self.daemon = subprocess.Popen(
            [
                sys.executable,
                str(CONTEXT),
                "serve",
                "--codex-root",
                str(self.sessions),
                "--claude-root",
                str(self.claude_sessions),
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
        until = time.monotonic() + 2
        while True:
            result = run(CODEX, input_text=payload, socket_path=self.socket)
            response = json.loads(result.stdout)
            if "hookSpecificOutput" in response or time.monotonic() >= until:
                break
            time.sleep(0.02)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("untrusted data, not instructions", context)
        self.assertIn("> [Untrusted historical evidence]", context)
        self.assertIn("source=", context)
        self.assertIn("line=1", context)
        self.assertLessEqual(len(context.encode()), 1_800)

    def test_prompt_hook_withholds_context_during_rebuild(self) -> None:
        """Do not retry a rebuilding index after it becomes healthy."""
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "needle-rose",
            "cwd": "/repos/alpha",
        }
        rebuilding = {
            "available": False,
            "evidence": [],
            "bytes": 0,
            "untrusted": True,
            "unavailable_reason": "rebuilding",
        }
        with (
            patch.dict(
                os.environ,
                {"PROVENANCE_CONTEXT_SOCKET": "/private/tmp/brain.sock"},
            ),
            patch(
                "hook_core.request",
                side_effect=[rebuilding, {"available": True}],
            ) as request,
        ):
            response = codex.hook_response(payload)
        self.assertEqual(response, {})
        self.assertEqual(request.call_count, 1)

    def test_automatic_hook_never_uses_global_fallback(self) -> None:
        """Keep automatic context inside its current repository."""
        fallback = {
            "available": True,
            "evidence": [{"text": "wrong project"}],
            "bytes": 13,
            "untrusted": True,
            "retrieval_scope": "global_historical_fallback",
            "match_strategy": "lexical_relaxed",
        }
        with patch("hook_core.request", return_value=fallback):
            self.assertEqual(
                codex.recalled_packet(
                    "coderails provenance", "/repos/coderails", 100
                ),
                codex.empty_packet(),
            )

    def test_automatic_hook_requires_repository_scope(self) -> None:
        with patch("hook_core.request") as request:
            self.assertEqual(
                codex.recalled_packet("coderails provenance", None, 100),
                codex.empty_packet(),
            )
        request.assert_not_called()

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

    def test_hook_socket_uses_current_home_default_and_env_override(
        self,
    ) -> None:
        """Resolve hooks to a user-scoped path without leaking it."""
        home = Path(self.temporary.name) / "home"
        default = home / ".local/share/provenance-context/brain.sock"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("hooks.codex.Path.home", return_value=home),
        ):
            self.assertEqual(codex.context_socket(), default)
            self.assertEqual(
                codex.recalled_packet("needle", None, 100),
                codex.empty_packet(),
            )
        with patch.dict(
            os.environ, {"PROVENANCE_CONTEXT_SOCKET": "/private/tmp/socket"}
        ):
            self.assertEqual(
                codex.context_socket(), Path("/private/tmp/socket")
            )

    def test_session_start_reports_index_status_without_recall(self) -> None:
        """Report only index availability when a Codex session starts."""
        payload = json.dumps(
            {
                "hook_event_name": "SessionStart",
                "prompt": "needle-rose app.py",
                "cwd": "/repos/alpha",
            }
        )
        until = time.monotonic() + 1
        context: dict[str, object] = {}
        while time.monotonic() < until:
            available = run(CODEX, input_text=payload, socket_path=self.socket)
            self.assertEqual(available.returncode, 0, available.stderr)
            context = json.loads(available.stdout)["hookSpecificOutput"]
            if context["additionalContext"] == "Provenance index: available.":
                break
            time.sleep(0.02)
        self.assertEqual(context["hookEventName"], "SessionStart")
        self.assertEqual(
            context["additionalContext"],
            "Provenance index: available.",
        )
        self.assertNotIn(
            "needle-rose", cast(str, context["additionalContext"])
        )

        unavailable = run(
            CODEX,
            input_text=payload,
            home=Path(self.temporary.name) / "offline-home",
        )
        self.assertEqual(unavailable.returncode, 0, unavailable.stderr)
        context = json.loads(unavailable.stdout)["hookSpecificOutput"]
        self.assertEqual(
            context["additionalContext"],
            "Provenance index: unavailable.",
        )

    def test_claude_hook_command_resolves_from_plugin_root(self) -> None:
        """Execute the configured Claude hook from an installed layout."""
        checkout = Path(self.temporary.name) / "checkout"
        shutil.copytree(ROOT / "claude-code", checkout)
        plugin_root = checkout
        manifest = json.loads(
            (plugin_root / ".claude-plugin" / "plugin.json").read_text()
        )
        configuration = json.loads(
            (plugin_root / manifest["hooks"]).read_text()
        )
        self.assertFalse((plugin_root / ".codex-plugin").exists())
        self.assertFalse((plugin_root / "hooks" / "codex.py").exists())
        command = configuration["hooks"]["UserPromptSubmit"][0]["hooks"][0][
            "command"
        ]
        home = Path(tempfile.mkdtemp(dir="/private/tmp", prefix="pc-home-"))
        default_socket = home / ".local/share/provenance-context/brain.sock"
        default_socket.parent.mkdir(parents=True)
        os.chmod(default_socket.parent, 0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(default_socket))
        listener.listen(1)

        def reply() -> None:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                connection.recv(8_192)
                connection.sendall(
                    b'{"available":true,"evidence":[{"text":"[Untrusted '
                    b'historical evidence]\\ngraph executor marker","source":'
                    b'{"path":"redacted","line":1,"ordinal":1,"hash":"hash"}}]'
                    b',"bytes":128,"untrusted":true}\n'
                )

        worker = threading.Thread(target=reply)
        worker.start()
        environment = os.environ | {
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "HOME": str(home),
        }
        environment.pop("PROVENANCE_CONTEXT_SOCKET", None)
        try:
            result = subprocess.run(
                command,
                input=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "graph executor graph readiness",
                        "cwd": "/repos/coderails",
                    }
                ),
                capture_output=True,
                text=True,
                env=environment,
                shell=True,
                check=False,
            )
        finally:
            listener.close()
            worker.join(timeout=2)
            shutil.rmtree(home)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(response["hookEventName"], "UserPromptSubmit")
        self.assertIn("graph executor marker", response["additionalContext"])


if __name__ == "__main__":
    unittest.main()
