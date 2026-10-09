"""Native Windows layout and confined installed-release selection."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from muninn import platform_io, platform_paths

SHA = "a" * 40


class WindowsLayoutTests(unittest.TestCase):
    """Explicit test homes cannot inherit the host's application directory."""

    def test_explicit_home_ignores_inherited_localappdata(self) -> None:
        home = Path("/isolated")
        self.assertEqual(
            platform_paths.windows_base({"LOCALAPPDATA": "/real"}, home=home),
            home / "AppData" / "Local" / "Muninn",
        )

    def test_environment_directory_has_priority_without_explicit_home(
        self,
    ) -> None:
        self.assertEqual(
            platform_paths.windows_base({"LOCALAPPDATA": "/local"}),
            Path("/local/Muninn"),
        )
        self.assertEqual(
            platform_paths.windows_base({"USERPROFILE": "/user"}),
            Path("/user/AppData/Local/Muninn"),
        )


class SelectionTests(unittest.TestCase):
    """Unsafe manifests never fall back to another installed release."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name) / "Muninn"
        self.lib = self.base / "lib"
        self.release = self.lib / SHA
        for path in (self.base, self.lib, self.release):
            platform_io.ensure_private_dir(path)
        self.manifest = self.lib / "selection.json"

    def write(self, value: object) -> None:
        """Write a private synthetic manifest."""
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        if sys.platform != "win32":
            self.manifest.chmod(0o600)

    def test_exact_selection_uses_recorded_interpreter(self) -> None:
        self.write({"sha": SHA, "python": sys.executable})
        self.assertEqual(
            platform_paths.read_selection(self.base),
            (self.release, Path(sys.executable)),
        )

    def test_malformed_or_missing_interpreter_is_refused(self) -> None:
        for value in (
            [],
            {"sha": "../outside", "python": sys.executable},
            {"sha": SHA, "python": "relative.exe"},
            {"sha": SHA, "python": sys.executable, "extra": True},
        ):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ValueError):
                    platform_paths.read_selection(self.base)

    def test_absent_selection_returns_none(self) -> None:
        self.assertIsNone(platform_paths.read_selection(self.base))

    def test_missing_recorded_interpreter_keeps_valid_release_selection(
        self,
    ) -> None:
        missing = self.base / "missing.exe"
        self.write({"sha": SHA, "python": str(missing)})
        self.assertEqual(
            platform_paths.read_selection(self.base), (self.release, missing)
        )
