"""Provider edits refuse links and retain content when publication fails."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install import config_windows, configedit


class ConfigSecurityTest(unittest.TestCase):
    """Original files are validated before reading or writing secrets."""

    def test_symlink_config_is_refused_without_editing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            original.write_bytes(b"secret")
            link = root / "settings.json"
            try:
                link.symlink_to(original)
            except OSError:
                return
            with self.assertRaises(OSError):
                configedit.edit_file(
                    link, lambda b: b + b"edited", lambda before, after: None
                )
            self.assertEqual(original.read_bytes(), b"secret")

    def test_failed_publication_retains_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_bytes(b"before")
            path.chmod(0o600)
            with (
                mock.patch.object(
                    configedit, "_publish", side_effect=OSError("busy")
                ),
                self.assertRaises(OSError),
            ):
                configedit.edit_file(
                    path, lambda b: b"after", lambda before, after: None
                )
            self.assertEqual(path.read_bytes(), b"before")
            self.assertEqual(list(path.parent.iterdir()), [path])

    @unittest.skipIf(
        os.name == "nt",
        "POSIX ownership and modes use native ACL checks on Windows",
    )
    def test_group_writable_config_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_bytes(b"before")
            path.chmod(0o666)
            with self.assertRaises(OSError):
                configedit.edit_file(
                    path, lambda b: b"after", lambda before, after: None
                )
            self.assertEqual(path.read_bytes(), b"before")

    def test_windows_ancestry_validates_parent_before_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_bytes(b"before")

            def parent_only(candidate: Path) -> None:
                self.assertEqual(candidate, path.parent)
                self.assertTrue(candidate.is_dir())
                raise RuntimeError("validated parent boundary")

            with (
                mock.patch.object(configedit.sys, "platform", "win32"),
                mock.patch.object(
                    configedit.platform_windows,
                    "assert_ancestry",
                    side_effect=parent_only,
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "validated parent"):
                    configedit.atomic_write(path, b"after", 0o600)
                with self.assertRaisesRegex(RuntimeError, "validated parent"):
                    config_windows.replacement(path)
            self.assertEqual(path.read_bytes(), b"before")
