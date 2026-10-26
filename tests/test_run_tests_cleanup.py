"""Preserve module evidence when native cleanup cannot complete."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import BinaryIO
from unittest import mock

from tools import run_tests, test_process


class CleanupOutputTests(unittest.TestCase):
    """Retain real child output independently of cleanup success."""

    def test_cleanup_failure_keeps_both_streams_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").touch()
            (root / "tests" / "test_output.py").write_text(
                "import sys,unittest\n"
                "print('captured stdout', flush=True)\n"
                "print('captured stderr', file=sys.stderr, flush=True)\n"
                "class T(unittest.TestCase):\n"
                " def test_ok(self): self.assertTrue(True)\n"
            )
            name = "windows_module" if os.name == "nt" else "posix_module"
            supervise = getattr(test_process, name)
            for error in (
                OSError("injected_cleanup_failure"),
                subprocess.TimeoutExpired("injected_cleanup_failure", 0.05),
            ):

                def fail_cleanup(
                    argv: tuple[str, ...],
                    root: Path,
                    env: dict[str, str],
                    deadline: float,
                    cancel: threading.Event,
                    streams: tuple[BinaryIO, BinaryIO],
                    error: OSError | subprocess.TimeoutExpired = error,
                ) -> tuple[int, bool]:
                    supervise(argv, root, env, deadline, cancel, streams)
                    raise error

                with (
                    self.subTest(error=type(error).__name__),
                    mock.patch.object(
                        test_process, name, side_effect=fail_cleanup
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    result = run_tests.run_module("tests.test_output", root)
                    self.assertEqual((result.returncode, result.ran), (127, 1))
                    self.assertIn("captured stdout", result.output)
                    self.assertIn("captured stderr", result.output)
                    self.assertIn("injected_cleanup_failure", result.output)


class QuiescenceTests(unittest.TestCase):
    """Keep an exited group leader reserved while descendants remain active."""

    @unittest.skipIf(os.name == "nt", "requires native POSIX process groups")
    def test_cleanup_refuses_success_while_a_descendant_is_active(
        self,
    ) -> None:
        if os.name == "nt":
            raise unittest.SkipTest("requires native POSIX process groups")
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready"
            identity = Path(temporary) / "child_pid"
            child = (
                "import os,time; from pathlib import Path; "
                f"Path({str(identity)!r}).write_text(str(os.getpid())); "
                f"Path({str(ready)!r}).touch(); "
                "time.sleep(60)"
            )
            leader = (
                "import subprocess,sys,time; from pathlib import Path; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                f"p=Path({str(ready)!r}); "
                "exec('while not p.exists(): time.sleep(.01)')"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", leader],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            kill_group = os.killpg
            try:
                until = time.monotonic() + 5
                exited = None
                while exited is None and time.monotonic() < until:
                    exited = os.waitid(
                        os.P_PID,
                        process.pid,
                        os.WEXITED | os.WNOHANG | os.WNOWAIT,
                    )
                    time.sleep(0.01)
                self.assertIsNotNone(exited)
                self.assertTrue(ready.exists())
                child_pid = int(identity.read_text())
                self.assertEqual(os.getpgid(child_pid), process.pid)
                start = time.monotonic()
                # Delay signal delivery while retaining a real active child.
                with (
                    mock.patch.object(test_process.os, "killpg"),
                    mock.patch.object(test_process, "CLEANUP_SECONDS", 0.1),
                    self.assertRaises(subprocess.TimeoutExpired),
                ):
                    test_process._contain(process)
                self.assertLess(time.monotonic() - start, 2)
                self.assertIsNone(process.returncode)
                self.assertEqual(os.getpgid(child_pid), process.pid)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    kill_group(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
