"""Measure reported-action containment without claiming action prevention."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from muninn import review_process as process
from muninn.review_process_identity import identify
from tests.test_review_worker import ATTEMPT, frame, policy, start_worker


class CodexStreamingQualificationTests(unittest.TestCase):
    """Use real owned processes with synthetic provider event streams."""

    def wait_event(
        self, monitor: process.ProcessMonitor, kind: str
    ) -> process.ProcessEvent:
        """Wait finitely for one observed phase without polling the child."""
        until = time.monotonic() + 15
        while time.monotonic() < until:
            for event in monitor.drain():
                if event.kind == kind:
                    return event
            time.sleep(0.01)
        raise AssertionError(f"missing process event: {kind}")

    def test_started_action_is_contained_before_provider_completion(
        self,
    ) -> None:
        """Detect a reported action before the provider finishes."""
        for action in (
            "command_execution",
            "mcp_tool_call",
            "web_search",
            "file_change",
        ):
            with (
                self.subTest(action=action),
                tempfile.TemporaryDirectory() as temporary,
            ):
                home = Path(temporary)
                marker = home / "action-already-happened"
                event = json.dumps(
                    {
                        "type": "item.started",
                        "item": {"id": "synthetic", "type": action},
                    }
                )
                script = (
                    "import time; from pathlib import Path; "
                    f"Path({str(marker)!r}).touch(); "
                    f"print({event!r}, flush=True); time.sleep(60)"
                )
                deadline = time.monotonic() + 20
                child = start_worker(deadline)
                running = process.RunningProcess(
                    ATTEMPT, 1, child, identify(child.pid), deadline, 4096
                )
                monitor = process.ProcessMonitor(policy(), runner="codex_cli")
                try:
                    monitor.register(running)
                    self.wait_event(monitor, "ready")
                    monitor.activate(ATTEMPT, 1, frame(script, home))
                    outcome = self.wait_event(monitor, "outcome")
                    self.assertIsInstance(outcome, process.ProcessOutcome)
                    assert isinstance(outcome, process.ProcessOutcome)
                    self.assertEqual(outcome.error_code, "native_tool_event")
                    self.assertEqual(outcome.stdout, b"")
                    self.assertIsNone(outcome.usage)
                    self.assertTrue(marker.exists())
                    self.assertIsNotNone(child.returncode)
                    self.assertLess(time.monotonic(), deadline)
                finally:
                    monitor.stop_all()
                    if child.returncode is None:
                        process.contain(running)
