"""Native writer lock contention, crash release and error classification."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from muninn import platform_lock

ROOT = Path(__file__).resolve().parent.parent
CHILD = """
import os, sys, time
from pathlib import Path
from muninn import platform_lock
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
platform_lock.try_lock(fd)
Path(sys.argv[2]).write_bytes(b'ready')
time.sleep(30)
"""


class NativeLockTests(unittest.TestCase):
    """Separate open descriptors conflict even inside the same process."""

    def test_second_open_conflicts_and_close_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "lock"
            first = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            second = os.open(path, os.O_RDWR)
            try:
                platform_lock.try_lock(first)
                with self.assertRaises(BlockingIOError):
                    platform_lock.try_lock(second)
                os.close(first)
                first = -1
                platform_lock.try_lock(second)
            finally:
                if first >= 0:
                    os.close(first)
                os.close(second)

    def test_bad_descriptor_is_not_contention(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fd = os.open(Path(temp) / "lock", os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            with self.assertRaises(OSError) as caught:
                platform_lock.try_lock(fd)
            self.assertNotIsInstance(caught.exception, BlockingIOError)

    def test_killed_writer_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path, ready = Path(temp) / "lock", Path(temp) / "ready"
            with subprocess.Popen(
                [sys.executable, "-c", CHILD, str(path), str(ready)], cwd=ROOT
            ) as child:
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        self.assertIsNone(child.poll())
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "writer never acquired")
                    fd = os.open(path, os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError):
                            platform_lock.try_lock(fd)
                        child.kill()
                        child.wait(timeout=10)
                        platform_lock.try_lock(fd)
                    finally:
                        os.close(fd)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=10)
