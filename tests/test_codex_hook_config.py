"""Installed Codex hook command contracts."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class CodexHookConfigTest(unittest.TestCase):
    """Verify the configured command survives plugin-cache substitution."""

    def test_session_hook_executes_substituted_plugin_root(self) -> None:
        """Run the exact configured command without provenance environment."""
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            plugin_root = root / "codex-cache"
            shutil.copytree(ROOT / "hooks", plugin_root / "hooks")
            shutil.copytree(
                ROOT / "claude-code" / "scripts",
                plugin_root / "claude-code" / "scripts",
            )
            command = json.loads(
                (plugin_root / "hooks" / "hooks.json").read_text()
            )["hooks"]["SessionStart"][0]["hooks"][0]["command"].replace(
                "${PLUGIN_ROOT}", str(plugin_root)
            )
            home = root / "home"
            socket_path = home / ".local/share/provenance-context/brain.sock"
            socket_path.parent.mkdir(parents=True)
            os.chmod(socket_path.parent, 0o700)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            worker = threading.Thread(target=self._reply, args=(listener,))
            worker.start()
            try:
                result = subprocess.run(
                    command,
                    input=json.dumps({"hook_event_name": "SessionStart"}),
                    capture_output=True,
                    text=True,
                    env={"HOME": str(home)},
                    shell=True,
                    check=False,
                )
            finally:
                listener.close()
                worker.join(timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Provenance index: available.", result.stdout)

    def test_hook_commands_are_distinct_executables(self) -> None:
        """Keep provider-specific hook paths out of each other's package."""
        codex = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        claude = json.loads(
            (ROOT / "claude-code" / "hooks" / "hooks.json").read_text()
        )
        codex_commands = self._commands(codex)
        claude_commands = self._commands(claude)
        self.assertTrue(
            all(
                command == '"${PLUGIN_ROOT}/hooks/codex.py"'
                for command in codex_commands
            )
        )
        self.assertTrue(
            all(
                command == '"${CLAUDE_PLUGIN_ROOT}/scripts/claude_context.py"'
                for command in claude_commands
            )
        )
        self.assertTrue(os.access(ROOT / "hooks" / "codex.py", os.X_OK))
        self.assertTrue(
            os.access(
                ROOT / "claude-code" / "scripts" / "claude_context.py",
                os.X_OK,
            )
        )

    @staticmethod
    def _commands(configuration: dict[str, object]) -> list[str]:
        """Return command strings from a hook manifest."""
        hooks = configuration["hooks"]
        if not isinstance(hooks, dict):
            return []
        return [
            hook["command"]
            for entries in hooks.values()
            if isinstance(entries, list)
            for entry in entries
            if isinstance(entry, dict)
            for hook in entry.get("hooks", [])
            if isinstance(hook, dict) and isinstance(hook.get("command"), str)
        ]

    @staticmethod
    def _reply(listener: socket.socket) -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            connection.recv(8_192)
            connection.sendall(b'{"available":true}\n')
