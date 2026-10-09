"""Portable subprocess readiness and timeout cleanup for native store tests."""

from __future__ import annotations

import unittest

from tests.store_support import Child


class ChildReadinessTests(unittest.TestCase):
    """Pipe readiness does not depend on select supporting file handles."""

    def test_ready_child_is_observed(self) -> None:
        child = Child(
            self, 'import time; print("ready",flush=True); time.sleep(30)'
        )
        child.wait_ready(timeout=5)
        self.assertIsNone(child.proc.poll())

    def test_silent_child_is_killed_at_timeout(self) -> None:
        child = Child(self, "import time; time.sleep(30)")
        with self.assertRaises(AssertionError):
            child.wait_ready(timeout=0.1)
        self.assertIsNotNone(child.proc.poll())
