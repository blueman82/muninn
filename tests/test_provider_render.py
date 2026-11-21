"""Provider rendering preserves native arguments and effective trust hashes."""

from __future__ import annotations

import base64
import json
import shlex
import tempfile
import unittest
from pathlib import Path

from install.context import Ctx, run_real
from install.provider_paths import cursor_command, render_pinned
from install.transforms import drop_cursor_settings, edit_cursor_settings
from install.trust import codex_hooks

ROOT = Path(__file__).resolve().parent.parent


class ProviderRenderTest(unittest.TestCase):
    """Installed hook paths survive shell metacharacters and Unicode."""

    def test_windows_claude_uses_real_executable_and_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(
                Path(tmp) / "space λ ' $ %",
                run_real,
                "test",
                platform="win32",
            )
            relative = "integrations/claude/settings-hooks.json"
            doc = json.loads(
                render_pinned(ctx, relative, (ROOT / relative).read_bytes())
            )
            handler = doc["hooks"]["SessionStart"][0]["hooks"][0]
            self.assertTrue(handler["command"].endswith("powershell.exe"))
            self.assertEqual(
                handler["args"][-5:],
                [
                    str(ctx.muninn.with_suffix(".ps1")),
                    "hook",
                    "session-start",
                    "--provider",
                    "claude",
                ],
            )
            self.assertEqual(handler["timeout"], 5)

    def test_windows_codex_command_encodes_shell_sensitive_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(
                Path(tmp) / "space λ ' $ %",
                run_real,
                "test",
                platform="win32",
            )
            relative = "integrations/codex/hooks/hooks.json"
            doc = json.loads(
                render_pinned(ctx, relative, (ROOT / relative).read_bytes())
            )
            command = doc["hooks"]["SessionStart"][0]["hooks"][0][
                "command_windows"
            ]
            script = base64.b64decode(command.split()[-1]).decode("utf-16-le")
            self.assertIn(
                str(ctx.muninn.with_suffix(".ps1")).replace("'", "''"), script
            )
            self.assertNotIn("%", command)

    def test_cursor_native_commands_preserve_paths_and_other_hooks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for platform in ("darwin", "linux", "win32"):
                with self.subTest(platform=platform):
                    ctx = Ctx(
                        Path(tmp) / "space λ ' $ %",
                        run_real,
                        "test",
                        platform=platform,
                    )
                    relative = "integrations/cursor/hooks.json"
                    rendered = render_pinned(
                        ctx, relative, (ROOT / relative).read_bytes()
                    )
                    handler = json.loads(rendered)["hooks"]["preCompact"][0]
                    self.assertEqual(handler["timeout"], 90)
                    command = cursor_command(ctx)
                    self.assertEqual(handler["command"], command)
                    if platform == "win32":
                        script = base64.b64decode(command.split()[-1]).decode(
                            "utf-16-le"
                        )
                        self.assertIn(
                            str(ctx.muninn.with_suffix(".ps1")).replace(
                                "'", "''"
                            ),
                            script,
                        )
                        self.assertIn(
                            "hook pre-compact --provider cursor", script
                        )
                        self.assertNotIn("%", command)
                    else:
                        self.assertEqual(
                            shlex.split(command),
                            [
                                str(ctx.muninn),
                                "hook",
                                "pre-compact",
                                "--provider",
                                "cursor",
                            ],
                        )
                    before = (
                        b'{"version":1,"hooks":{"preCompact":'
                        b'[{"command":"other"}],"afterFileEdit":'
                        b'[{"command":"keep"}]}}'
                    )
                    after = edit_cursor_settings(
                        before,
                        {"preCompact": command},
                        {"preCompact": [handler]},
                    )
                    restored = drop_cursor_settings(
                        after, {"preCompact": command}
                    )
                    self.assertEqual(json.loads(restored), json.loads(before))
                    self.assertEqual(
                        json.loads(
                            edit_cursor_settings(
                                after,
                                {"preCompact": command},
                                {"preCompact": [handler]},
                            )
                        ),
                        json.loads(after),
                    )

    def test_trust_hash_uses_effective_windows_override(self) -> None:
        doc = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "posix",
                                "command_windows": "windows",
                            }
                        ]
                    }
                ]
            }
        }
        windows = codex_hooks(json.dumps(doc).encode(), platform="win32")
        ordinary = codex_hooks(json.dumps(doc).encode(), platform="linux")
        self.assertEqual(windows[0]["command"], "windows")
        self.assertNotEqual(windows[0]["hash"], ordinary[0]["hash"])
