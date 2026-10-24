"""Check platform and path boundaries for the native Codex reviewer."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock, skipIf

from muninn import review_runner
from muninn.review_codex_launch import (
    _check_executable_storage,
    verify_codex_executable,
)


class CodexPathBoundaryTests(TestCase):
    """Refuse paths and platforms outside the approved Codex boundary."""

    def test_relative_executable_path_is_refused(self) -> None:
        """Resolve stable links only from an explicit absolute path."""
        with self.assertRaisesRegex(ValueError, "capability_path"):
            verify_codex_executable(Path("codex"))

    def test_windows_private_path_refused(self) -> None:
        """Reject private-path checks on unsupported Windows hosts."""
        with (
            mock.patch.object(review_runner.sys, "platform", "win32"),
            self.assertRaisesRegex(ValueError, "capability_platform"),
        ):
            review_runner.check_private_path(Path("/unused"), directory=True)

    @skipIf(sys.platform == "win32", "POSIX executable path contract")
    def test_publicly_writable_path_is_refused(self) -> None:
        """An updater without Codex auth must not replace the binary."""
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary) / "public-bin"
            public.mkdir(mode=0o700)
            public.chmod(0o777)
            executable = public / "codex"
            executable.write_bytes(b"synthetic")
            executable.chmod(0o755)
            with (
                mock.patch(
                    "muninn.review_codex_launch._has_acl", return_value=False
                ),
                self.assertRaisesRegex(
                    ValueError, "capability_executable_storage"
                ),
            ):
                _check_executable_storage(executable)
