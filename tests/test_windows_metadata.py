"""Native ancestor trust and same-handle private metadata refusals."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from muninn import (
    platform_io,
    platform_paths,
    platform_windows,
    poller_stop,
    tombstone_key,
)

if sys.platform == "win32":

    class WindowsMetadataTests(unittest.TestCase):
        """Broaden isolated ACLs at the exact check/read boundary."""

        def setUp(self) -> None:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            self.home = Path(temp.name) / "private"
            platform_io.ensure_private_dir(self.home)

        def grant(self, path: Path, rights: str) -> None:
            """Add one effective Everyone rule to a temporary object."""
            subprocess.run(
                ["icacls", str(path), "/grant", f"*S-1-1-0:({rights})"],
                capture_output=True,
                check=True,
            )

        def write(self, path: Path, data: bytes) -> None:
            """Write synthetic metadata through private native creation."""
            fd = platform_io.open_private(path, os.O_CREAT | os.O_RDWR)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)

        def broaden_on_open(self, path: Path, **kwargs: object) -> object:
            """Change the actual ACL after the earlier pathname check."""
            self.grant(path, "R")
            return self.original_open(path)

        def test_unsafe_ancestor_refuses_before_directory_creation(
            self,
        ) -> None:
            parent = self.home / "parent"
            platform_io.ensure_private_dir(parent)
            self.grant(parent, "DC")
            child = parent / "not-created"
            with self.assertRaises(PermissionError):
                platform_io.ensure_private_dir(child)
            self.assertFalse(child.exists())

        def test_read_public_executable_is_not_write_public(self) -> None:
            executable = self.home / "interpreter.exe"
            self.write(executable, b"synthetic")
            self.grant(executable, "RX")
            platform_windows.assert_executable(executable)
            self.grant(executable, "W")
            with self.assertRaises(PermissionError):
                platform_windows.assert_executable(executable)

        def test_key_acl_broadened_after_path_check_is_not_read(self) -> None:
            path = self.home / tombstone_key.KEY_FILE
            self.write(path, b"k" * 32)
            self.original_open = platform_io.open_regular
            with (
                patch.object(
                    platform_io, "open_regular", self.broaden_on_open
                ),
                self.assertRaises(PermissionError),
            ):
                tombstone_key.load_key(self.home)
            self.assertEqual(path.read_bytes(), b"k" * 32)

        def test_stop_acl_broadened_after_path_check_is_not_accepted(
            self,
        ) -> None:
            poller_stop.request(self.home, 123, "a" * 32)
            self.original_open = platform_io.open_regular
            with patch.object(
                platform_io, "open_regular", self.broaden_on_open
            ):
                self.assertFalse(
                    poller_stop.requested(self.home, 123, "a" * 32)
                )

        def test_selection_acl_broadened_after_path_check_is_not_read(
            self,
        ) -> None:
            lib = self.home / "lib"
            platform_io.ensure_private_dir(lib)
            sha = "a" * 40
            platform_io.ensure_private_dir(lib / sha)
            self.write(
                lib / "selection.json",
                json.dumps({"sha": sha, "python": sys.executable}).encode(),
            )
            self.original_open = platform_io.open_regular
            with (
                patch.object(
                    platform_io, "open_regular", self.broaden_on_open
                ),
                self.assertRaises(ValueError),
            ):
                platform_paths.read_selection(self.home)
