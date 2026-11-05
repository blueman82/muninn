"""Keep managed signature work inside the original finite deadline."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase, mock

from muninn import review_codex_launch as launch


class CodexDeadlineTests(TestCase):
    """Use a disposable file and refuse before any native verifier work."""

    def test_managed_verifier_starts_no_work_after_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "fake-codex"
            executable.write_bytes(b"fixture")
            with (
                mock.patch("time.monotonic", return_value=10.0),
                mock.patch("subprocess.run") as native,
                self.assertRaisesRegex(
                    ValueError, "runtime_probe_unavailable"
                ),
            ):
                launch.verify_codex_executable(
                    executable, limit=384 * 1024 * 1024, deadline=9.0
                )
            native.assert_not_called()
