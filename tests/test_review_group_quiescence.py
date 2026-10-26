"""Keep real group members contained before reaping an exited wrapper."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from muninn import review_process as process
from muninn.review_launch_spec import worker_launch
from muninn.review_process_identity import identify
from tests.test_review_worker import ATTEMPT, policy


@unittest.skipIf(os.name == "nt", "requires POSIX process groups")
class GroupQuiescenceTests(unittest.TestCase):
    """Use owned native children without relying on kernel signal timing."""

    home: Path
    witness: Path
    wrapper: subprocess.Popen[bytes]
    handle: process.RunningProcess
    monitor: process.ProcessMonitor
    member_pid: int | None
    member_identity: process.ProcessIdentity | None

    def setUp(self) -> None:
        if os.name == "nt":
            raise unittest.SkipTest("requires POSIX process groups")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.witness = self.home / "member"
        self.member_pid: int | None = None
        self.member_identity: process.ProcessIdentity | None = None
        deadline = time.monotonic() + 10
        command, environment = worker_launch(deadline, 8192, 4096)
        script = (
            "import os,signal,time; from pathlib import Path; "
            "until=time.monotonic()+10; "
            f"p=Path({str(self.witness)!r}); "
            f"Path({str(self.home / 'member.pid')!r})"
            ".write_text(str(os.getpid())); "
            "signal.signal(signal.SIGUSR1, lambda *_: os.setpgid(0,0)); "
            "exec('while time.monotonic()<until: "
            "p.write_text(str(time.monotonic())); "
            "time.sleep(.01)')"
        )
        bootstrap = (
            "import subprocess,sys,runpy; "
            f"subprocess.Popen([sys.executable,'-c',{script!r}], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL); "
            "sys.argv=['muninn.review_worker',*sys.argv[1:]]; "
            "runpy.run_module('muninn.review_worker',run_name='__main__')"
        )
        command = (*command[:3], "-c", bootstrap, *command[5:])
        with (self.home / "stderr").open("wb") as errors:
            self.wrapper = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                close_fds=True,
                start_new_session=True,
            )
        self.handle = process.RunningProcess(
            ATTEMPT,
            1,
            self.wrapper,
            identify(self.wrapper.pid),
            deadline,
            4096,
        )
        self.monitor = process.ProcessMonitor(
            policy().model_copy(update={"deadline_seconds": 3.0})
        )
        self.addCleanup(self.cleanup)
        self.monitor.register(self.handle)

    def cleanup(self) -> None:
        """Stop the identified member without relying on a released PGID."""
        if os.name == "nt":
            return
        if self.member_pid is not None:
            try:
                if identify(self.member_pid) == self.member_identity:
                    os.kill(self.member_pid, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
        self.monitor.stop_all()

    def events(self, kind: str) -> tuple[process.ProcessEvent, ...]:
        """Wait a finite interval for an actual monitor lifecycle event."""
        found: list[process.ProcessEvent] = []
        until = time.monotonic() + 5
        while time.monotonic() < until:
            found.extend(self.monitor.drain())
            if any(event.kind == kind for event in found):
                return tuple(found)
            time.sleep(0.005)
        raise AssertionError(f"missing lifecycle phase {kind}")

    def start_member(self) -> None:
        """Observe the real descendant created inside the isolated session."""
        until = time.monotonic() + 2
        while time.monotonic() < until:
            try:
                if self.witness.read_bytes():
                    self.member_pid = int(
                        (self.home / "member.pid").read_text()
                    )
                    identity = identify(self.member_pid)
                    self.member_identity = identity
                    self.assertEqual(identity.pgid, self.wrapper.pid)
                    return
            except FileNotFoundError:
                pass
            time.sleep(0.005)
        raise AssertionError("group member did not write its witness")

    def test_delayed_signal_keeps_outcome_pending_before_reap(self) -> None:
        if os.name == "nt":
            raise unittest.SkipTest("requires POSIX process groups")
        self.events("ready")
        self.start_member()
        attempted = threading.Event()
        deliver = threading.Event()
        real_killpg, real_wait = os.killpg, self.wrapper.wait
        waits: list[bool] = []

        def killpg(pgid: int, sig: int) -> None:
            self.assertEqual(pgid, self.wrapper.pid)
            attempted.set()
            if deliver.is_set():
                real_killpg(pgid, sig)

        def wait(timeout: float | None = None) -> int:
            waits.append(deliver.is_set())
            return real_wait(timeout)

        with (
            mock.patch.object(process.os, "killpg", side_effect=killpg),
            mock.patch.object(self.wrapper, "wait", side_effect=wait),
            mock.patch.object(
                self.wrapper, "poll", side_effect=AssertionError("hidden poll")
            ),
        ):
            try:
                os.kill(self.wrapper.pid, signal.SIGKILL)
                self.assertTrue(attempted.wait(2))
                found: list[process.ProcessEvent] = []
                until = time.monotonic() + 0.05
                while time.monotonic() < until:
                    found.extend(self.monitor.drain())
                    time.sleep(0.005)
                self.assertFalse(
                    any(event.kind == "outcome" for event in found),
                    "premature outcome before group quiescence",
                )
                self.assertEqual(waits, [])
                self.assertIsNone(self.wrapper.returncode)
                deliver.set()
                outcome = self.events("outcome")[-1]
            finally:
                deliver.set()
        self.assertIsInstance(outcome, process.ProcessOutcome)
        assert isinstance(outcome, process.ProcessOutcome)
        self.assertEqual(outcome.error_code, "worker_eof")
        self.assertEqual(self.wrapper.returncode, -signal.SIGKILL)
        self.assertTrue(all(waits))
        last = self.witness.read_bytes()
        time.sleep(0.05)
        self.assertEqual(self.witness.read_bytes(), last)

    @unittest.skipUnless(sys.platform == "linux", "Linux session observation")
    def test_live_same_session_other_group_keeps_outcome_pending(self) -> None:
        if sys.platform != "linux":
            raise unittest.SkipTest("requires Linux session observation")
        self.events("ready")
        self.start_member()
        assert self.member_pid is not None
        os.kill(self.member_pid, signal.SIGUSR1)
        until = time.monotonic() + 2
        while os.getpgid(self.member_pid) != self.member_pid:
            if time.monotonic() >= until:
                self.fail("member did not leave the original group")
            time.sleep(0.005)
        with (
            mock.patch.object(
                self.wrapper, "wait", wraps=self.wrapper.wait
            ) as wait,
            mock.patch.object(
                self.wrapper, "poll", side_effect=AssertionError("hidden poll")
            ),
        ):
            try:
                os.kill(self.wrapper.pid, signal.SIGKILL)
                time.sleep(0.05)
                self.assertEqual(self.monitor.drain(), ())
                wait.assert_not_called()
                self.assertIsNone(self.wrapper.returncode)
                self.assertEqual(os.getsid(self.member_pid), self.wrapper.pid)
            finally:
                os.kill(self.member_pid, signal.SIGKILL)
            outcome = self.events("outcome")[-1]
        self.assertIsInstance(outcome, process.ProcessOutcome)
        assert isinstance(outcome, process.ProcessOutcome)
        self.assertEqual(outcome.error_code, "worker_eof")
