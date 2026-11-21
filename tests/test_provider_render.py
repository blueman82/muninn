"""Provider rendering preserves native arguments."""

from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

from install.context import Ctx, run_real
from install.provider_paths import cursor_command, render_pinned
from install.transforms import drop_cursor_settings, edit_cursor_settings

ROOT = Path(__file__).resolve().parent.parent


class ProviderRenderTest(unittest.TestCase):
    """Installed hook paths survive shell metacharacters and Unicode."""

    def test_cursor_native_commands_preserve_paths_and_other_hooks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp) / "space λ ' $ %", run_real, "test")
            relative = "integrations/cursor/hooks.json"
            rendered = render_pinned(
                ctx, relative, (ROOT / relative).read_bytes()
            )
            handler = json.loads(rendered)["hooks"]["preCompact"][0]
            self.assertEqual(handler["timeout"], 90)
            command = cursor_command(ctx)
            self.assertEqual(handler["command"], command)
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
                before, {"preCompact": command}, {"preCompact": [handler]}
            )
            restored = drop_cursor_settings(after, {"preCompact": command})
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
