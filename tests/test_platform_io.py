"""Shared safe opens preserve binary bytes and reject substituted sources."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from muninn import platform_io


class SafeOpenTests(unittest.TestCase):
    """Read exactly the originally discovered regular file."""

    def test_binary_bytes_are_not_translated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source"
            data = b"one\r\ntwo\x1a\n"
            path.write_bytes(data)
            with platform_io.open_regular(path, root=Path(temp)) as handle:
                self.assertEqual(handle.read(), data)

    def test_outside_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            root.mkdir()
            path = Path(temp) / "outside"
            path.write_bytes(b"outside")
            with self.assertRaises(OSError):
                platform_io.open_regular(path, root=root)

    def test_directory_is_refused(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaises(OSError),
        ):
            platform_io.open_regular(Path(temp))

    def test_replaced_file_identity_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path, swap = Path(temp) / "source", Path(temp) / "swap"
            path.write_bytes(b"original")
            before = path.stat()
            swap.write_bytes(b"replacement")
            swap.replace(path)
            with self.assertRaises(OSError):
                platform_io.open_regular(path, expected=before)

    def test_private_file_write_uses_binary_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "private"
            platform_io.ensure_private_dir(home)
            fd = platform_io.open_private(
                home / "file", os.O_CREAT | os.O_RDWR
            )
            try:
                os.write(fd, b"raw\n")
            finally:
                os.close(fd)
            self.assertEqual((home / "file").read_bytes(), b"raw\n")
