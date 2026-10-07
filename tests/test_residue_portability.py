"""Residue scans need not read empty files, including native locked ranges."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn.erase_residue import residue_scan


class EmptyFileScanTests(unittest.TestCase):
    """Empty files cannot hold a needle; all nonempty files stay covered."""

    def test_empty_file_is_not_read_and_nonempty_file_is_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            empty = home / "empty"
            empty.touch()
            (home / "state").write_bytes(b"retained canary")
            real = Path.read_bytes

            def read(path: Path) -> bytes:
                if path == empty:
                    raise PermissionError("synthetic mandatory range lock")
                return real(path)

            with mock.patch.object(Path, "read_bytes", read):
                self.assertEqual(residue_scan(home, [b"canary"]), ["state"])
