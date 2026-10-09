"""Cursor rollback restores only owned entries in current user settings."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from install import configedit as ce
from install import rollback, steps_config
from install.provider_paths import cursor_command
from install.transforms import edit_cursor_settings
from tests.installer_support import World


class CursorRollbackTests(unittest.TestCase):
    """Exercise atomic Cursor rollback with late foreign changes."""

    def setUp(self) -> None:
        self.w = World(self)
        self.ctx = self.w.ctx(cursor_hooks=True)
        self.path = self.ctx.cursor_settings
        self.command = cursor_command(self.ctx)

    def install_config(self, before: dict[str, Any] | None) -> None:
        """Snapshot and apply only the Cursor configuration install step."""
        self.path.parent.mkdir(parents=True)
        if before is not None:
            self.path.write_text(json.dumps(before))
        self.rec = {
            "has_claude": False,
            "has_codex": False,
            "cursor_hooks": True,
        }
        with mock.patch.object(
            steps_config, "_codex_observation", return_value=("", ())
        ):
            steps_config.record(self.ctx, self.rec)
        source = self.path.read_bytes() if before is not None else b"{}"
        self.path.write_bytes(
            edit_cursor_settings(
                source, self.command, {"command": self.command, "timeout": 90}
            )
        )

    def restore(self) -> dict[str, Any] | None:
        """Run the guarded rollback and return the remaining config."""
        rollback._undo_config(self.ctx, self.rec)
        return (
            json.loads(self.path.read_bytes()) if self.path.exists() else None
        )

    def test_late_foreign_changes_and_prior_owned_metadata_survive(
        self,
    ) -> None:
        owned = {"command": self.command, "timeout": 15, "metadata": "old"}
        self.install_config(
            {
                "version": 1,
                "hooks": {
                    "preCompact": [
                        {"command": "deleted"},
                        owned,
                        {"command": "edited", "n": 1},
                    ]
                },
                "setting": "before",
            }
        )
        current = json.loads(self.path.read_bytes())
        current["hooks"]["preCompact"] = [
            {"command": "edited", "n": 2},
            {"command": self.command, "timeout": 90},
            {"command": "late"},
        ]
        current["hooks"]["afterFileEdit"] = [{"command": "keep"}]
        current["setting"] = "after"
        self.path.write_text(json.dumps(current))
        expected = current | {
            "hooks": {
                "preCompact": [
                    {"command": "edited", "n": 2},
                    owned,
                    {"command": "late"},
                ],
                "afterFileEdit": [{"command": "keep"}],
            }
        }
        self.assertEqual(self.restore(), expected)
        self.assertEqual(self.restore(), expected)

    def test_fresh_config_with_late_foreign_hook_is_retained(self) -> None:
        self.install_config(None)
        current = json.loads(self.path.read_bytes())
        current["hooks"]["preCompact"].append({"command": "late"})
        self.path.write_text(json.dumps(current))
        self.assertEqual(
            self.restore(),
            {"version": 1, "hooks": {"preCompact": [{"command": "late"}]}},
        )

    def test_empty_fresh_config_is_removed_and_repeat_is_safe(self) -> None:
        self.install_config(None)
        self.assertIsNone(self.restore())
        self.assertIsNone(self.restore())

    def test_existing_empty_file_is_retained(self) -> None:
        self.install_config({})
        self.assertEqual(self.restore(), {})
        self.assertTrue(self.path.exists())

    def test_foreign_version_update_is_preserved(self) -> None:
        self.install_config({"version": 1})
        current = json.loads(self.path.read_bytes())
        current["version"] = 2
        self.path.write_text(json.dumps(current))
        self.assertEqual(self.restore(), {"version": 2})

    def test_foreign_hooks_retain_required_version(self) -> None:
        self.install_config(
            {"hooks": {"afterFileEdit": [{"command": "keep"}]}}
        )
        self.assertEqual(
            self.restore(),
            {"version": 1, "hooks": {"afterFileEdit": [{"command": "keep"}]}},
        )

    def test_malformed_current_config_refuses_without_changes(self) -> None:
        self.install_config(None)
        for hooks in ([], {"preCompact": {}}, {"preCompact": [42]}):
            with self.subTest(hooks=hooks):
                source = json.dumps({"version": 1, "hooks": hooks}).encode()
                self.path.write_bytes(source)
                with self.assertRaises(ce.RefusedError):
                    self.restore()
                self.assertEqual(self.path.read_bytes(), source)
