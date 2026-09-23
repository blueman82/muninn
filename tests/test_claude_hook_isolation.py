"""Claude Code's hook discovery set must never contain Codex hooks."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
IGNORED = shutil.ignore_patterns(".git", "__pycache__", ".ruff_cache")


def claude_hook_commands(plugin_root: Path) -> list[str]:
    """Return every hook command Claude Code loads for this plugin root.

    Claude always loads ``<root>/hooks/hooks.json`` by convention and *adds*
    whatever path ``plugin.json``'s ``hooks`` field names; the field cannot
    suppress the conventional file.
    """
    manifests = [plugin_root / "hooks" / "hooks.json"]
    declared = json.loads(
        (plugin_root / ".claude-plugin" / "plugin.json").read_text()
    ).get("hooks")
    if isinstance(declared, str):
        manifests.append(plugin_root / declared)
    commands: list[str] = []
    for manifest in manifests:
        if not manifest.is_file():
            continue
        for matchers in json.loads(manifest.read_text())["hooks"].values():
            for matcher in matchers:
                commands.extend(hook["command"] for hook in matcher["hooks"])
    return commands


class ClaudeHookIsolationTest(unittest.TestCase):
    """Verify the installed Claude plugin never reaches a Codex hook."""

    def test_discovered_commands_are_claude_only_and_resolve(self) -> None:
        """Claude loads only claude_context.py, never codex.py."""
        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "provenance-context" / "0.1.1"
            shutil.copytree(ROOT / "claude-code", plugin_root, ignore=IGNORED)
            commands = claude_hook_commands(plugin_root)
            self.assertTrue(commands, "Claude discovered no hook commands")
            for command in commands:
                with self.subTest(command=command):
                    self.assertNotIn("${PLUGIN_ROOT}", command)
                    self.assertIn("${CLAUDE_PLUGIN_ROOT}", command)
                    executable = Path(
                        shlex.split(
                            command.replace(
                                "${CLAUDE_PLUGIN_ROOT}", str(plugin_root)
                            )
                        )[0]
                    )
                    self.assertEqual(executable.name, "claude_context.py")
                    self.assertTrue(os.access(executable, os.X_OK), executable)


if __name__ == "__main__":
    unittest.main()
