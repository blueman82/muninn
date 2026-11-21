"""Check process running with fake executables only."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from muninn.review_call import CallOutcome, run_call


def _script(tmp: Path, body: str) -> str:
    path = tmp / "fake"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


class RunCallTests(unittest.TestCase):
    """Each failure maps to one content-free code."""

    def run_body(
        self, body: str, timeout: float = 5, limit: int = 100
    ) -> CallOutcome:
        with tempfile.TemporaryDirectory() as tmp:
            exe = _script(Path(tmp), body)
            return run_call(
                [exe],
                b"prompt",
                Path(tmp),
                {"PATH": os.defpath},
                timeout,
                limit,
            )

    def test_success_echoes_stdin(self) -> None:
        out = self.run_body("cat\n")
        self.assertEqual((out.stdout, out.error_code), (b"prompt", None))

    def test_nonzero_exit(self) -> None:
        self.assertEqual(self.run_body("exit 3\n").error_code, "cli_failed")

    def test_output_limit(self) -> None:
        self.assertEqual(
            self.run_body("cat\n", limit=3).error_code, "output_limit"
        )

    def test_hang_is_killed_with_its_children(self) -> None:
        start = time.monotonic()
        out = self.run_body("sleep 30 &\nsleep 30\n", timeout=0.5)
        self.assertEqual(out.error_code, "timeout")
        self.assertLess(time.monotonic() - start, 10)

    def test_missing_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = run_call(["/nonexistent/x"], b"", Path(tmp), {}, 1, 10)
        self.assertEqual(out.error_code, "cli_missing")


if __name__ == "__main__":
    unittest.main()
