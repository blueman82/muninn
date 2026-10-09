"""Release directory publication is explicit and never replaces a winner."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install import release_io
from muninn import platform_io, platform_windows


class DirectoryDispatchTests(unittest.TestCase):
    """Preserve ordinary file defaults at the installation boundary."""

    def test_install_dispatches_explicit_nonreplacing_directory(self) -> None:
        source, target = Path("/synthetic/source"), Path("/synthetic/target")
        with (
            patch.object(release_io.os, "name", "nt"),
            patch.object(platform_windows, "publish", create=True) as publish,
        ):
            release_io.publish(source, target, replace=False, directory=True)
        publish.assert_called_once_with(
            source, target, replace=False, directory=True
        )

    def test_directory_replacement_refuses_before_native_effects(self) -> None:
        source, target = Path("/synthetic/source"), Path("/synthetic/target")
        with (
            patch.object(platform_windows, "publish", create=True) as publish,
            self.assertRaises(ValueError),
        ):
            release_io.publish(source, target, replace=True, directory=True)
        publish.assert_not_called()


if sys.platform == "win32":

    class NativeDirectoryPublicationTests(unittest.TestCase):
        """Exercise real same-volume native directory ACL and reparse rules."""

        def setUp(self) -> None:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.home = Path(temporary.name) / "private"
            platform_io.ensure_private_dir(self.home)
            self.source, self.target = (
                self.home / "source",
                self.home / "target",
            )
            platform_io.ensure_private_dir(self.source)
            fd = platform_io.open_private(
                self.source / "state", os.O_CREAT | os.O_RDWR
            )
            with os.fdopen(fd, "wb") as output:
                output.write(b"complete")
                output.flush()
                os.fsync(output.fileno())

        def test_complete_private_directory_moves_without_copy(self) -> None:
            platform_windows.publish(
                self.source, self.target, replace=False, directory=True
            )
            self.assertFalse(self.source.exists())
            self.assertEqual((self.target / "state").read_bytes(), b"complete")
            platform_windows.assert_private(self.target, directory=True)

        def test_existing_directory_keeps_both_complete_trees(self) -> None:
            platform_io.ensure_private_dir(self.target)
            (self.target / "winner").write_bytes(b"winner")
            with self.assertRaises(FileExistsError):
                platform_windows.publish(
                    self.source, self.target, replace=False, directory=True
                )
            self.assertEqual((self.source / "state").read_bytes(), b"complete")
            self.assertEqual((self.target / "winner").read_bytes(), b"winner")

        def test_unsafe_source_refuses_without_moving(self) -> None:
            subprocess.run(
                ["icacls", str(self.source), "/grant", "*S-1-1-0:(R)"],
                capture_output=True,
                check=True,
            )
            with self.assertRaises(OSError):
                platform_windows.publish(
                    self.source, self.target, replace=False, directory=True
                )
            self.assertTrue(self.source.exists())
            self.assertFalse(self.target.exists())

        def test_unsafe_target_refuses_and_keeps_source(self) -> None:
            platform_io.ensure_private_dir(self.target)
            subprocess.run(
                ["icacls", str(self.target), "/grant", "*S-1-1-0:(R)"],
                capture_output=True,
                check=True,
            )
            with self.assertRaises(OSError):
                platform_windows.publish(
                    self.source, self.target, replace=False, directory=True
                )
            self.assertEqual((self.source / "state").read_bytes(), b"complete")
            self.assertTrue(self.target.is_dir())

        def test_directory_replacement_never_moves_either_tree(self) -> None:
            platform_io.ensure_private_dir(self.target)
            with self.assertRaises(ValueError):
                platform_windows.publish(
                    self.source, self.target, replace=True, directory=True
                )
            self.assertEqual((self.source / "state").read_bytes(), b"complete")
            self.assertTrue(self.target.is_dir())

        def test_directory_reparse_refuses_without_moving_target(self) -> None:
            junction = self.home / "junction"
            subprocess.run(
                [
                    "cmd.exe",
                    "/c",
                    "mklink",
                    "/J",
                    str(junction),
                    str(self.source),
                ],
                capture_output=True,
                check=True,
            )
            self.addCleanup(junction.rmdir)
            with self.assertRaises(OSError):
                platform_windows.publish(
                    junction, self.target, replace=False, directory=True
                )
            self.assertEqual((self.source / "state").read_bytes(), b"complete")
            self.assertFalse(self.target.exists())
