"""Optional Cursor setup choices and native installed-release selection."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from install import steps_config as sc
from tests.installer_support import World


class CursorChoiceTest(unittest.TestCase):
    """The installer explains automatic and manual Cursor setup."""

    def setUp(self) -> None:
        self.w = World(self)

    def test_cursor_hook_reads_selected_native_release(self) -> None:
        ctx = self.w.ctx()
        ctx.cursor_hooks = True
        release = self.w.home / "selected-release"
        template = release / "integrations/cursor/hooks.json"
        template.parent.mkdir(parents=True)
        template.write_bytes(
            (self.w.repo / "integrations/cursor/hooks.json")
            .read_bytes()
            .replace(b"@HOME@", str(ctx.home).encode())
        )
        with mock.patch.object(
            type(ctx),
            "release",
            new_callable=mock.PropertyMock,
            return_value=release,
        ):
            sc.cursor(ctx, {"cursor_hooks": True})
        installed = json.loads(ctx.cursor_settings.read_bytes())
        self.assertEqual(installed["hooks"]["preCompact"][0]["timeout"], 90)

    def test_noninteractive_install_defaults_to_manual_and_shows_location(
        self,
    ) -> None:
        with mock.patch.object(
            sc.sys, "stdin", mock.Mock(isatty=lambda: False)
        ):
            self.assertFalse(sc.choose_cursor_hooks(self.w.ctx()))
        said = "\n".join(self.w.out)
        self.assertIn(
            f"Cursor hook target: {self.w.home}/.cursor/hooks.json", said
        )
        self.assertIn("docs/QUICKSTART.md#cursor-history", said)
        self.assertIn("left for manual setup", said)

    def test_interactive_choice_accepts_automatic_or_manual(self) -> None:
        for answer, expected in (("a", True), ("m", False)):
            self.w.out.clear()
            with (
                mock.patch.object(
                    sc.sys, "stdin", mock.Mock(isatty=lambda: True)
                ),
                mock.patch("builtins.input", return_value=answer) as prompt,
            ):
                self.assertEqual(
                    sc.choose_cursor_hooks(self.w.ctx()), expected
                )
            prompt.assert_called_once()
            self.assertIn("[a]utomatic or [m]anual", prompt.call_args.args[0])
