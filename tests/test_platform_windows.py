"""Actual Windows ACL, handle and publication behavior in temporary homes."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from muninn import platform_io, platform_windows
from muninn.file_sync import sync_fd

if sys.platform == "win32":

    class WindowsFileTests(unittest.TestCase):
        """Check ACL refusals and complete-file sharing failures."""

        def setUp(self) -> None:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            self.home = Path(temp.name) / "private"
            platform_io.ensure_private_dir(self.home)

        def write(self, name: str, data: bytes) -> Path:
            """Create and flush private synthetic state."""
            path = self.home / name
            fd = platform_io.open_private(path, os.O_CREAT | os.O_RDWR)
            try:
                os.write(fd, data)
                sync_fd(fd)
            finally:
                os.close(fd)
            return path

        def grant_everyone(self, path: Path) -> None:
            """Broaden only the temporary object's ACL using a literal SID."""
            subprocess.run(
                ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],
                check=True,
                capture_output=True,
            )

        def test_private_directory_and_file_acl(self) -> None:
            path = self.write("state", b"private")
            platform_windows.assert_private(self.home, directory=True)
            platform_windows.assert_private(path)

        def test_unsafe_existing_directory_is_refused(self) -> None:
            self.grant_everyone(self.home)
            with self.assertRaises(PermissionError):
                platform_io.open_private(
                    self.home / "state", os.O_CREAT | os.O_RDWR
                )
            self.assertFalse((self.home / "state").exists())

        def test_unsafe_file_is_refused_before_any_write(self) -> None:
            path = self.write("state", b"original")
            self.grant_everyone(path)
            with self.assertRaises(PermissionError):
                platform_io.open_private(path, os.O_WRONLY | os.O_APPEND)
            self.assertEqual(path.read_bytes(), b"original")

        def test_nonreplacing_publication_keeps_the_complete_winner(
            self,
        ) -> None:
            winner, loser = self.write("key", b"w" * 32), self.write(
                "temp", b"l" * 32
            )
            with self.assertRaises(FileExistsError):
                platform_windows.publish(loser, winner, replace=False)
            self.assertEqual(winner.read_bytes(), b"w" * 32)
            self.assertEqual(loser.read_bytes(), b"l" * 32)

        def test_publication_replaces_with_a_complete_synced_file(
            self,
        ) -> None:
            target, source = self.write("state", b"old"), self.write(
                "temp", b"new"
            )
            platform_windows.publish(source, target, replace=True)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(source.exists())
            platform_windows.assert_private(target)

        def test_unshared_reader_preserves_old_file(
            self,
        ) -> None:
            target, source = self.write("state", b"old"), self.write(
                "temp", b"new"
            )
            with target.open("rb") as handle:
                with self.assertRaises(OSError):
                    platform_windows.publish(source, target, replace=True)
                self.assertEqual(handle.read(), b"old")
                self.assertEqual(target.read_bytes(), b"old")
                self.assertEqual(source.read_bytes(), b"new")

        def test_shared_reader_replacement_or_refusal_preserves_bytes(
            self,
        ) -> None:
            target, source = self.write("state", b"old"), self.write(
                "temp", b"new"
            )
            refused = False
            with platform_io.open_regular(target) as handle:
                try:
                    platform_windows.publish(source, target, replace=True)
                except OSError:
                    refused = True
                    self.assertEqual(target.read_bytes(), b"old")
                    self.assertEqual(source.read_bytes(), b"new")
                self.assertEqual(handle.read(), b"old")
            if refused:
                platform_windows.publish(source, target, replace=True)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(source.exists())

        def test_junction_escape_is_refused(self) -> None:
            outside = self.home.parent / "outside"
            outside.mkdir()
            (outside / "source").write_bytes(b"outside")
            junction = self.home / "junction"
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                check=True,
                capture_output=True,
            )
            with self.assertRaises(OSError):
                platform_io.open_regular(junction / "source", root=self.home)
            junction.rmdir()
