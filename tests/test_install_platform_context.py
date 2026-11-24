"""Isolated native installer paths and platform defaults."""

from __future__ import annotations

import unittest
from pathlib import Path

from install.context import Ctx, run_real


class PlatformContextTests(unittest.TestCase):
    """Require explicit installer homes to ignore inherited host paths."""

    def test_macos_paths_remain_unchanged(self) -> None:
        home = Path("/synthetic/home")
        ctx = Ctx(home, run_real, "test", uid=501)
        self.assertEqual(ctx.target, "gui/501/com.muninn")
        self.assertEqual(
            ctx.plist, home / "Library/LaunchAgents/com.muninn.plist"
        )
