"""Isolated native installer paths and platform defaults."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install.context import Ctx, run_real


class PlatformContextTests(unittest.TestCase):
    """Require explicit installer homes to ignore inherited host paths."""

    def test_windows_explicit_home_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.dict(os.environ, {"LOCALAPPDATA": "/outside"}):
                ctx = Ctx(home, run_real, "test", platform="win32")
            base = home / "AppData/Local/Muninn"
            self.assertEqual(ctx.data, base / "data")
            self.assertEqual(ctx.lib, base / "lib")
            self.assertEqual(ctx.muninn, base / "bin/muninn.cmd")
            self.assertEqual(ctx.config, home / ".codex/config.toml")

    def test_linux_has_user_service_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            ctx = Ctx(home, run_real, "test", platform="linux")
            self.assertEqual(ctx.data, home / ".local/share/muninn")
            self.assertEqual(
                ctx.plist, home / ".config/systemd/user/muninn.service"
            )
            self.assertEqual(ctx.target, "muninn.service")

    def test_macos_paths_remain_unchanged(self) -> None:
        home = Path("/synthetic/home")
        ctx = Ctx(home, run_real, "test", uid=501, platform="darwin")
        self.assertEqual(ctx.target, "gui/501/com.muninn")
        self.assertEqual(
            ctx.plist, home / "Library/LaunchAgents/com.muninn.plist"
        )
