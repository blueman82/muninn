"""Platform provider roots, explicit Cursor input and foreign scope safety."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import cursor_import, ingest, scope


class ProviderPathsTest(unittest.TestCase):
    """Only native paths and explicit provider overrides are used."""

    def test_provider_overrides_are_respected(self) -> None:
        roots = ingest.default_roots(
            {
                "HOME": "/temporary/home",
                "CODEX_HOME": "/temporary/codex",
                "CLAUDE_CONFIG_DIR": "/temporary/claude",
            }
        )
        self.assertEqual(
            roots["codex-sessions"], Path("/temporary/codex/sessions")
        )
        self.assertEqual(
            roots["claude-projects"], Path("/temporary/claude/projects")
        )

    def test_windows_profile_precedes_inherited_home(self) -> None:
        with mock.patch("muninn.ingest.sys.platform", "win32"):
            roots = ingest.default_roots(
                {"HOME": "/wrong", "USERPROFILE": "/profile"}
            )
        self.assertEqual(
            roots["codex-sessions"], Path("/profile/.codex/sessions")
        )

    def test_foreign_absolute_cwd_never_uses_commit_hint(self) -> None:
        foreign = "/home/foreign" if os.name == "nt" else "C:\\Users\\foreign"
        with mock.patch.object(scope, "_has_commit") as probe:
            self.assertEqual(
                scope.resolve_key(foreign, "a" * 40, repos=["fake"]),
                ("unknown", "dir", "cwd"),
            )
        probe.assert_not_called()

    def test_unc_cwd_is_unknown_without_network_resolution(self) -> None:
        with mock.patch.object(Path, "resolve") as resolve:
            self.assertEqual(
                scope.resolve_key(r"\\server\share\project"),
                ("unknown", "dir", "cwd"),
            )
        resolve.assert_not_called()

    def test_cursor_explicit_path_and_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            explicit = home / "Unicode λ" / "state.vscdb"
            self.assertEqual(
                cursor_import.default_database(
                    home, env={"MUNINN_CURSOR_DB": str(explicit)}
                ),
                explicit,
            )
            with self.assertRaises(ValueError):
                cursor_import.default_database(
                    home, env={"MUNINN_CURSOR_DB": "relative/state.vscdb"}
                )

    def test_non_mac_cursor_requires_explicit_input(self) -> None:
        with mock.patch("muninn.cursor_import.sys.platform", "linux"):
            self.assertIsNone(
                cursor_import.default_database(Path("/home/temp"), env={})
            )
