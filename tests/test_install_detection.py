"""Installed apps, rather than leftover settings, select provider setup."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from install import installer as co
from install.provider_apps import installed
from install.steps_config import choose_cursor_hooks
from tests.installer_support import World


class AppDetectionTest(unittest.TestCase):
    """Exercise app presence without consulting the machine's real apps."""

    def setUp(self) -> None:
        self.w = World(self)

    def test_leftover_settings_do_not_select_absent_apps(self) -> None:
        for provider in ("Claude", "Codex", "Cursor"):
            path = self.w.home / "Applications" / f"{provider}.app"
            if path.exists():
                path.rmdir()
        self.w.ctx().settings.write_text("malformed leftover JSON")
        self.w.ctx().config.write_text("malformed leftover TOML")
        real_is_dir = Path.is_dir
        with mock.patch.object(
            Path,
            "is_dir",
            autospec=True,
            side_effect=lambda path: (
                False if path.suffix == ".app" else real_is_dir(path)
            ),
        ):
            rec = co.install(self.w.ctx(fresh=True), self.w.repo, self.w.sha)
        self.assertFalse(rec["has_claude"])
        self.assertFalse(rec["has_codex"])
        self.assertIn("Claude app not installed: setup skipped", self.w.out)
        self.assertIn("Codex app not installed: setup skipped", self.w.out)
        self.assertFalse(
            any("--provider" in call for call in self.w.fake.calls)
        )

    def test_installed_apps_without_config_receive_manual_guidance(
        self,
    ) -> None:
        self.w.ctx().settings.unlink()
        self.w.ctx().config.unlink()
        co.install(self.w.ctx(fresh=True), self.w.repo, self.w.sha)
        output = "\n".join(self.w.out)
        self.assertIn("Claude installed; manual setup", output)
        self.assertIn("Codex installed; manual setup", output)

    def test_windows_user_apps_without_path_entries(self) -> None:
        ctx = dataclasses.replace(self.w.ctx(), platform="win32")
        local = self.w.home / "AppData/Local"
        for provider, relative in (
            ("claude", "AnthropicClaude/claude.exe"),
            ("cursor", "Programs/cursor/Cursor.exe"),
            ("codex", "Programs/Codex/Codex.exe"),
        ):
            path = local / relative
            path.parent.mkdir(parents=True)
            path.touch()
            with (
                mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}),
                mock.patch(
                    "install.provider_apps.shutil.which", return_value=None
                ),
            ):
                self.assertTrue(installed(ctx, provider), provider)

    def test_absent_cursor_does_not_prompt(self) -> None:
        (self.w.home / "Applications/Cursor.app").rmdir()
        real_is_dir = Path.is_dir
        with (
            mock.patch.object(
                Path,
                "is_dir",
                autospec=True,
                side_effect=lambda path: (
                    False if path.suffix == ".app" else real_is_dir(path)
                ),
            ),
            mock.patch(
                "install.steps_config.sys.stdin.isatty", return_value=True
            ),
            mock.patch(
                "builtins.input", side_effect=AssertionError("prompted")
            ),
        ):
            self.assertFalse(choose_cursor_hooks(self.w.ctx()))
        self.assertNotIn("Cursor hook target", "\n".join(self.w.out))


class PackageDetectionTest(unittest.TestCase):
    """Registered Windows apps and desktop launchers provide evidence."""

    def test_windows_system_apps_and_absent_cli_only(self) -> None:
        w = World(self)
        ctx = dataclasses.replace(w.ctx(), platform="win32")
        programs = w.home / "Program Files"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "ProgramFiles": str(programs),
                    "ProgramFiles(x86)": str(programs),
                },
            ),
            mock.patch.object(
                ctx,
                "run",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ),
            mock.patch(
                "install.provider_apps.shutil.which",
                return_value="cli-only.exe",
            ),
        ):
            for provider, name in (
                ("claude", "Claude"),
                ("codex", "Codex"),
                ("cursor", "Cursor"),
            ):
                self.assertFalse(installed(ctx, provider))
                executable = programs / name / f"{name}.exe"
                executable.parent.mkdir(parents=True)
                executable.touch()
                self.assertTrue(installed(ctx, provider))

    def test_windows_registered_packages_require_payload(self) -> None:
        w = World(self)
        ctx = dataclasses.replace(w.ctx(), platform="win32")
        payload = w.home / "package"
        payload.mkdir()
        manifest = payload / "AppxManifest.xml"
        for provider, package in (
            ("claude", "Claude"),
            ("codex", "OpenAI.Codex"),
        ):
            raw = json.dumps(
                {"Name": package, "InstallLocation": str(payload)}
            ).encode()
            with mock.patch.object(
                ctx,
                "run",
                return_value=subprocess.CompletedProcess([], 0, raw, b""),
            ):
                self.assertFalse(installed(ctx, provider))
                manifest.write_text(
                    "<Package><Applications>"
                    '<Application Executable="app.exe" />'
                    "</Applications></Package>"
                )
                self.assertFalse(installed(ctx, provider))
                executable = payload / "app.exe"
                executable.touch()
                self.assertTrue(installed(ctx, provider))
                executable.unlink()
                manifest.unlink()

    def test_linux_uses_desktop_launchers(self) -> None:
        w = World(self)
        ctx = dataclasses.replace(w.ctx(), platform="linux")
        for provider, command in (
            ("claude", "claude-desktop"),
            ("codex", "chatgpt"),
            ("cursor", "cursor"),
        ):
            with mock.patch(
                "install.provider_apps.shutil.which",
                side_effect=lambda name, command=command: (
                    "/installed/app" if name == command else None
                ),
            ):
                self.assertTrue(installed(ctx, provider))
