"""Keep cleanup refusals pending and preserve the cause after containment."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

from muninn import review_process as process
from muninn.review_process_identity import identify
from tests.test_review_worker import ATTEMPT, policy, start_worker


class ReviewProcessCleanupTests(unittest.TestCase):
    """Exercise cleanup retries with owned disposable process groups."""

    def setUp(self) -> None:
        self.child = start_worker(time.monotonic() + 60)
        self.handle = process.RunningProcess(
            ATTEMPT,
            1,
            self.child,
            identify(self.child.pid),
            time.monotonic() - 1,
            4096,
        )
        self.monitor = process.ProcessMonitor(
            policy().model_copy(update={"deadline_seconds": 5.0})
        )
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        """Restore the owned identity before joining the real monitor."""
        with self.monitor.lock:
            slot = self.monitor.slots.get((ATTEMPT, 1))
            if slot is not None:
                slot.handle = self.handle
        self.monitor.limits = policy().model_copy(
            update={"deadline_seconds": 5.0}
        )
        self.monitor.stop_all()
        if self.child.returncode is None:
            process.contain(self.handle)

    def test_transient_reap_timeout_keeps_successful_terminal_outcome(
        self,
    ) -> None:
        slot = process._Slot(self.handle)
        slot.finished = True
        slot.exit_code = 0
        real_contain = process.contain
        attempts = 0

        def delayed_reap(handle: process.RunningProcess) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise subprocess.TimeoutExpired("worker", 0.01)
            return real_contain(handle)

        observed = process.ObservedConfig(
            runner=None, version=None, model=None, reasoning=None
        )
        with (
            mock.patch.object(process, "contain", side_effect=delayed_reap),
            mock.patch.object(
                self.monitor,
                "_decode",
                return_value=(b"success", observed, None),
            ),
        ):
            self.monitor._finish(slot, None)
            self.assertIsNone(slot.terminal)
            self.assertIsNone(slot.error)
            self.monitor._step(slot, False)
        self.assertEqual(attempts, 2)
        self.assertIsNotNone(slot.terminal)
        assert slot.terminal is not None
        self.assertIsNone(slot.terminal.error_code)
        self.assertEqual(slot.terminal.stdout, b"success")

    def test_transient_reap_timeout_keeps_prior_protocol_error(self) -> None:
        slot = process._Slot(self.handle)
        real_contain = process.contain
        attempts = 0

        def delayed_reap(handle: process.RunningProcess) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise subprocess.TimeoutExpired("worker", 0.01)
            return real_contain(handle)

        with mock.patch.object(process, "contain", side_effect=delayed_reap):
            self.monitor._finish(slot, "invalid_worker_protocol")
            self.assertIsNone(slot.terminal)
            self.assertTrue(slot.finished)
            self.assertEqual(slot.error, "invalid_worker_protocol")
            self.monitor._step(slot, False)
        self.assertEqual(attempts, 2)
        self.assertIsNotNone(slot.terminal)
        assert slot.terminal is not None
        self.assertEqual(slot.terminal.error_code, "invalid_worker_protocol")

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_transient_permission_error_preserves_deadline_after_reap(
        self,
    ) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX ownership or process APIs")
        retry_entered = threading.Event()
        allow_retry = threading.Event()
        real_killpg = os.killpg
        attempts = 0

        def killpg(pgid: int, sig: int) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError(errno.EPERM, "injected refusal")
            retry_entered.set()
            if not allow_retry.wait(5):
                raise OSError("test_retry_not_released")
            real_killpg(pgid, sig)

        with (
            mock.patch.object(process.os, "killpg", killpg),
            mock.patch.object(process, "sole_zombie", return_value=False),
        ):
            self.monitor.register(self.handle)
            try:
                self.assertTrue(retry_entered.wait(5))
                self.assertIsNone(self.child.returncode)
                self.assertEqual(self.monitor.drain(), ())
                self.assertEqual(
                    identify(self.child.pid), self.handle.identity
                )
            finally:
                allow_retry.set()
            self.monitor.stop_all()
        events = self.monitor.drain()
        self.assertEqual(len(events), 1)
        outcome = events[0]
        self.assertIsInstance(outcome, process.ProcessOutcome)
        assert isinstance(outcome, process.ProcessOutcome)
        self.assertEqual(outcome.error_code, "deadline")
        self.assertEqual(self.child.returncode, -signal.SIGKILL)
        self.assertEqual(self.monitor.drain(), ())

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_persistent_permission_error_keeps_cleanup_incomplete(
        self,
    ) -> None:
        self.monitor.limits = policy().model_copy(
            update={"deadline_seconds": 0.05}
        )
        with (
            mock.patch.object(
                process.os,
                "killpg",
                side_effect=PermissionError(errno.EPERM, "injected refusal"),
            ),
            mock.patch.object(process, "sole_zombie", return_value=False),
        ):
            self.monitor.register(self.handle)
            with self.assertRaisesRegex(OSError, "process_cleanup_incomplete"):
                self.monitor.stop_all()
            self.assertEqual(self.monitor.drain(), ())
            self.assertEqual(len(self.monitor.slots), 1)
            self.assertIsNone(self.child.returncode)
            self.assertEqual(identify(self.child.pid), self.handle.identity)

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_uncertain_identity_keeps_cleanup_incomplete_without_signals(
        self,
    ) -> None:
        wrong = replace(
            self.handle,
            identity=self.handle.identity.model_copy(
                update={"pgid": self.child.pid + 1}
            ),
        )
        self.monitor.limits = policy().model_copy(
            update={"deadline_seconds": 0.05}
        )
        with (
            mock.patch.object(process.os, "killpg") as killpg,
            mock.patch.object(self.child, "wait") as wait,
        ):
            self.monitor.register(wrong)
            with self.assertRaisesRegex(OSError, "process_cleanup_incomplete"):
                self.monitor.stop_all()
            self.assertEqual(self.monitor.drain(), ())
            self.assertEqual(len(self.monitor.slots), 1)
            self.assertIsNone(self.child.returncode)
            killpg.assert_not_called()
            wait.assert_not_called()
