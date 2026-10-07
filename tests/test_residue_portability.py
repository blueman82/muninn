"""Residue scans need not read empty files, including native locked ranges."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import BinaryIO
from unittest import mock

from muninn import platform_io
from muninn.erase_residue import residue_scan


class EmptyFileScanTests(unittest.TestCase):
    """Empty files cannot hold a needle; all nonempty files stay covered."""

    def test_empty_file_is_not_read_and_nonempty_file_is_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            empty = home / "empty"
            empty.touch()
            (home / "state").write_bytes(b"retained canary")
            real = platform_io.open_regular

            def read(
                path: Path,
                *,
                root: Path | None = None,
                expected: os.stat_result | None = None,
            ) -> BinaryIO:
                if path == empty:
                    raise PermissionError("synthetic mandatory range lock")
                return real(path, root=root, expected=expected)

            with mock.patch.object(platform_io, "open_regular", read):
                self.assertEqual(residue_scan(home, [b"canary"]), ["state"])

    def test_changed_descriptor_identity_is_refused_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "private"
            platform_io.ensure_private_dir(home)
            path, replacement = home / "state", Path(temp) / "replacement"
            path.write_bytes(b"original canary")
            replacement.write_bytes(b"replacement canary")
            original_open = platform_io.open_regular

            def swap(
                source: Path,
                *,
                root: Path | None = None,
                expected: os.stat_result | None = None,
            ) -> BinaryIO:
                replacement.replace(source)
                return original_open(source, root=root, expected=expected)

            with (
                mock.patch.object(platform_io, "open_regular", swap),
                self.assertRaises(OSError),
            ):
                residue_scan(home, [b"canary"])
