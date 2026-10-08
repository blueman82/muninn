"""CI interpreter copying changes only its private copy."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests import lifecycle_native_python


class PrivatePythonTests(unittest.TestCase):
    """Expose code relocation without executing synthetic candidates."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.prefix = self.parent / "source"
        self.exe = self.prefix / "bin/python3.13"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"synthetic executable")
        self.exe.chmod(0o775)
        self.library = self.prefix / "lib/python3.13/os.py"
        self.library.parent.mkdir(parents=True)
        self.library.write_bytes(b"synthetic stdlib")
        self.dynamic = self.prefix / "lib/libpython3.13.so.1.0"
        self.dynamic.write_bytes(b"synthetic dynamic library")
        self.target = self.parent / "private"
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_copy_retains_prefix_and_host_bytes_without_host_chmod(
        self,
    ) -> None:
        before = self.exe.stat().st_mode, self.exe.read_bytes()
        real_chmod = Path.chmod
        changes: list[tuple[Path, int]] = []

        def chmod(
            path: Path, mode: int, *, follow_symlinks: bool = True
        ) -> None:
            changes.append((path, mode))
            real_chmod(path, mode, follow_symlinks=follow_symlinks)

        with patch.object(Path, "chmod", chmod):
            copied = lifecycle_native_python.prepare(
                self.prefix, self.exe, self.target, self.exe.stat().st_uid
            )
        self.assertEqual(copied, self.target / "bin/python3.13")
        self.assertEqual(
            (self.exe.stat().st_mode, self.exe.read_bytes()), before
        )
        self.assertEqual(
            (self.target / "lib/python3.13/os.py").read_bytes(),
            self.library.read_bytes(),
        )
        self.assertEqual(
            (self.target / "lib/libpython3.13.so.1.0").read_bytes(),
            self.dynamic.read_bytes(),
        )
        self.assertTrue(changes)
        self.assertTrue(
            all(path.is_relative_to(self.target) for path, _ in changes)
        )
        self.assertIn((copied, 0o700), changes)

    def test_unknown_copy_destination_is_retained(self) -> None:
        self.target.mkdir()
        marker = self.target / "unknown"
        marker.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            lifecycle_native_python.prepare(
                self.prefix, self.exe, self.target, self.exe.stat().st_uid
            )
        self.assertEqual(marker.read_bytes(), b"keep")

    def test_unrecognized_source_refuses_before_copy(self) -> None:
        for candidate in (self.exe.parent, self.parent / "absent"):
            with self.subTest(candidate=candidate), self.assertRaises(OSError):
                lifecycle_native_python.prepare(
                    self.prefix, candidate, self.target, 0
                )
        self.assertFalse(self.target.exists())
