"""Check finite wrappers with disposable local children and explicit limits."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from muninn import review_process as process
from muninn import review_process_identity as identity
from muninn import review_process_protocol as protocol
from muninn.review_models import ReviewPolicy
from tests.test_review_shared_models import policy_data

ATTEMPT = "A_" + "2" * 32
ROOT = Path(__file__).resolve().parent.parent
SUCCESS = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {"op": "search"},
    }
)


def policy() -> ReviewPolicy:
    """Supply explicit generous local-test byte limits."""
    return ReviewPolicy.model_validate(
        {**policy_data(), "max_input_bytes": 8192, "max_output_bytes": 4096}
    )


def start_worker(
    deadline: float, stderr_path: Path | None = None
) -> subprocess.Popen[bytes]:
    """Start only the finite wrapper with no inherited descriptors."""
    with (
        stderr_path.open("wb") if stderr_path else Path(os.devnull).open("wb")
    ) as errors:
        return subprocess.Popen(
            [
                sys.executable,
                "-E",
                "-s",
                "-m",
                "muninn.review_worker",
                "--deadline",
                str(deadline),
                "--max-input-bytes",
                "8192",
                "--max-output-bytes",
                "4096",
            ],
            cwd=ROOT,
            env={},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            close_fds=True,
            start_new_session=True,
        )


def frame(script: str, cwd: Path) -> bytes:
    """Encode trusted parent data for a disposable Python child."""
    return protocol.activation(
        (sys.executable, "-c", script), cwd, {}, b"fixture prompt", 8192
    )


class ReviewWorkerTests(unittest.TestCase):
    """Prove handshake, byte bounds, containment and descriptor exclusion."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.worker_stderr = self.home / "worker-stderr.txt"
        self.monitor = process.ProcessMonitor(policy())
        self.addCleanup(self.monitor.stop_all)

    def worker(self, seconds: float = 5.0) -> process.RunningProcess:
        """Adopt a local wrapper before any activation bytes are sent."""
        deadline = time.monotonic() + seconds
        child = start_worker(deadline, self.worker_stderr)
        running = process.RunningProcess(
            ATTEMPT, 1, child, identity.identify(child.pid), deadline, 4096
        )
        self.monitor.register(running)
        return running

    def events(self, kind: str) -> tuple[process.ProcessEvent, ...]:
        """Drain finite events until the expected lifecycle phase arrives."""
        found: list[process.ProcessEvent] = []
        until = time.monotonic() + 8
        while time.monotonic() < until:
            found.extend(self.monitor.drain())
            if any(event.kind == kind for event in found):
                return tuple(found)
            time.sleep(0.01)
        detail = (
            self.worker_stderr.read_text(errors="replace")[:4096]
            if self.worker_stderr.exists()
            else ""
        )
        phases = [
            (event.kind, getattr(event, "error_code", None)) for event in found
        ]
        raise AssertionError(
            f"missing lifecycle phase {kind}; events={phases!r}; "
            f"worker stderr={detail!r}"
        )

    def test_registration_precedes_first_cli_launch(self) -> None:
        self.worker()
        self.assertEqual([e.kind for e in self.events("ready")], ["ready"])
        marker = self.home / "launched"
        self.assertFalse(marker.exists())
        self.monitor.activate(
            ATTEMPT,
            1,
            frame(
                f"from pathlib import Path; Path({str(marker)!r}).touch(); "
                f"print({SUCCESS!r})",
                self.home,
            ),
        )
        events = self.events("outcome")
        self.assertEqual([e.kind for e in events], ["launched", "outcome"])
        self.assertTrue(marker.exists())
        self.assertEqual(
            cast(process.ProcessOutcome, events[-1]).error_code, None
        )

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_parent_eof_stops_descendants(self) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX ownership or process APIs")
        running = self.worker()
        self.events("ready")
        self.monitor.activate(
            ATTEMPT, 1, frame("import time; time.sleep(30)", self.home)
        )
        launched = self.events("launched")[-1]
        self.assertIsInstance(launched, process.WorkerLaunched)
        assert running.wrapper.stdin is not None
        running.wrapper.stdin.close()
        self.events("outcome")
        self.assertIsNotNone(running.wrapper.returncode)
        with self.assertRaises(ProcessLookupError):
            os.killpg(running.wrapper.pid, 0)

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_dead_wrapper_group_killed_before_reap(self) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX ownership or process APIs")
        running = self.worker()
        self.events("ready")
        witness = self.home / "descendant"
        script = (
            "import time; from pathlib import Path; "
            f"p=Path({str(witness)!r}); "
            "exec('while True: p.write_text(str(time.monotonic())); "
            "time.sleep(.01)')"
        )
        self.monitor.activate(ATTEMPT, 1, frame(script, self.home))
        self.events("launched")
        until = time.monotonic() + 2
        while not witness.exists() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue(witness.exists())
        order: list[str] = []
        real_killpg, real_wait = os.killpg, running.wrapper.wait

        def killpg(pgid: int, sig: int) -> None:
            order.append("group")
            real_killpg(pgid, sig)

        def wait(timeout: float | None = None) -> int:
            order.append("reap")
            self.assertIn("group", order)
            return real_wait(timeout)

        with (
            mock.patch.object(process.os, "killpg", killpg),
            mock.patch.object(running.wrapper, "wait", wait),
            mock.patch.object(
                running.wrapper, "poll", side_effect=AssertionError("poll")
            ),
        ):
            os.kill(running.wrapper.pid, signal.SIGKILL)
            self.events("outcome")
        self.assertEqual(order[0], "group")
        last = witness.read_bytes()
        time.sleep(0.05)
        self.assertEqual(witness.read_bytes(), last)

    @unittest.skipIf(sys.platform == "win32", "fcntl is POSIX-only")
    def test_cli_launch_inherits_no_writer_descriptor(self) -> None:
        from_script = (
            "import fcntl, os; "
            f"fd=os.open({str(self.home / 'writer.lock')!r},"
            "os.O_CREAT|os.O_RDWR); "
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); "
            f"print({SUCCESS!r})"
        )
        self.worker()
        self.events("ready")
        self.monitor.activate(ATTEMPT, 1, frame(from_script, self.home))
        outcome = cast(process.ProcessOutcome, self.events("outcome")[-1])
        self.assertIsInstance(outcome, process.ProcessOutcome)
        self.assertEqual(outcome.exit_code, 0)

    def test_aggregate_stdout_stderr_overflow_stops_owned_wrapper(
        self,
    ) -> None:
        self.worker()
        self.events("ready")
        self.monitor.activate(
            ATTEMPT,
            1,
            frame(
                "import os,time; os.write(1,b'x'*2048); "
                "os.write(2,b'x'*2049); time.sleep(30)",
                self.home,
            ),
        )
        outcome = cast(process.ProcessOutcome, self.events("outcome")[-1])
        self.assertEqual(outcome.error_code, "output_limit")
        self.assertEqual(outcome.stdout, b"")

    def test_activation_rejects_invalid_lengths_and_header(self) -> None:
        good = frame("pass", self.home)
        for raw in (
            good[:8],
            (2**64 - 1).to_bytes(8, "big") + good[8:],
            good[:8] + (2**32 - 1).to_bytes(4, "big") + good[12:],
        ):
            with self.subTest(raw=raw[:12]), self.assertRaises(ValueError):
                protocol.decode_activation(raw, 8192)
        argv, cwd, env, prompt = protocol.decode_activation(good, 8192)
        self.assertEqual(
            (argv[0], cwd, env, prompt),
            (sys.executable, self.home, {}, b"fixture prompt"),
        )

    def test_registration_failure_before_adoption_is_contained(self) -> None:
        deadline = time.monotonic() + 5
        child = start_worker(deadline)
        running = process.RunningProcess(
            ATTEMPT, 1, child, identity.identify(child.pid), deadline, 4096
        )
        process.adopt_after_registration_error(self.monitor, running)
        events = self.events("outcome")
        self.assertEqual([event.kind for event in events], ["outcome"])
        self.assertEqual(
            cast(process.ProcessOutcome, events[-1]).error_code,
            "registration_failed",
        )
        self.assertIsNotNone(child.returncode)

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_unrelated_identity_is_refused_without_poll_or_signal(
        self,
    ) -> None:
        running = self.worker()
        self.events("ready")
        wrong = process.RunningProcess(
            ATTEMPT,
            1,
            running.wrapper,
            running.identity.model_copy(
                update={"pgid": running.wrapper.pid + 1}
            ),
            running.deadline,
            running.output_limit,
        )
        with (
            mock.patch.object(process.os, "killpg") as killpg,
            mock.patch.object(running.wrapper, "wait") as wait,
            self.assertRaisesRegex(OSError, "process_identity_unverified"),
        ):
            process.contain(wrong)
        killpg.assert_not_called()
        wait.assert_not_called()

    def test_inheritable_parent_descriptor_does_not_reach_cli(self) -> None:
        with (self.home / "sentinel").open("wb") as sentinel:
            fd = sentinel.fileno()
            os.set_inheritable(fd, True)
            stat = os.fstat(fd)
            self.worker()
            self.events("ready")
            check = (
                f"try:\n s=os.fstat({fd})\n "
                f"assert (s.st_dev,s.st_ino) != {(stat.st_dev, stat.st_ino)!r}"
                "\nexcept OSError: pass"
            )
            script = f"import os; exec({check!r}); print({SUCCESS!r})"
            self.monitor.activate(ATTEMPT, 1, frame(script, self.home))
            result = cast(process.ProcessOutcome, self.events("outcome")[-1])
            self.assertIsNone(result.error_code)

    def test_all_capacity_terminals_remain_until_drain(self) -> None:
        monitor = process.ProcessMonitor(
            policy().model_copy(update={"concurrency": 2})
        )
        self.addCleanup(monitor.stop_all)
        handles: list[process.RunningProcess] = []
        for digit in ("3", "4"):
            deadline = time.monotonic() + 5
            child = start_worker(deadline)
            handle = process.RunningProcess(
                "A_" + digit * 32,
                1,
                child,
                identity.identify(child.pid),
                deadline,
                4096,
            )
            handles.append(handle)
            monitor.register(handle)
            process.cancel_registered(monitor, handle.attempt_id, 1, "stopped")
        time.sleep(0.2)
        self.assertEqual(len(monitor.slots), 2)
        with self.assertRaisesRegex(ValueError, "monitor_capacity"):
            monitor.register(handles[0])
        terminal = monitor.drain()
        self.assertEqual(sum(event.kind == "outcome" for event in terminal), 2)
        self.assertTrue(
            all(handle.wrapper.returncode is not None for handle in handles)
        )
        self.assertEqual(monitor.drain(), ())

    def test_recovery_preserves_terminal_and_rejects_other_handle(
        self,
    ) -> None:
        running = self.worker()
        process.cancel_registered(self.monitor, ATTEMPT, 1, "stopped")
        time.sleep(0.2)
        slot = self.monitor.slots[ATTEMPT, 1]
        self.assertIsNotNone(slot.terminal)
        process.adopt_after_registration_error(self.monitor, running)
        self.assertIs(self.monitor.slots[ATTEMPT, 1].terminal, slot.terminal)
        other = process.RunningProcess(
            ATTEMPT,
            1,
            running.wrapper,
            running.identity,
            running.deadline,
            running.output_limit,
        )
        with self.assertRaisesRegex(
            RuntimeError, "monitor_ownership_invariant"
        ):
            process.adopt_after_registration_error(self.monitor, other)
        self.assertIs(self.monitor.slots[ATTEMPT, 1].handle, running)

    def test_empty_arguments_reach_real_child(self) -> None:
        self.worker()
        self.assertEqual([e.kind for e in self.events("ready")], ["ready"])
        script = (
            "import sys; sys.stdin.buffer.read(); "
            "assert sys.argv[1:] == ['--tools', '']; "
            f"print({SUCCESS!r})"
        )
        self.monitor.activate(
            ATTEMPT,
            1,
            protocol.activation(
                (sys.executable, "-c", script, "--tools", ""),
                self.home,
                {},
                b"synthetic",
                8192,
            ),
        )
        events = self.events("outcome")
        self.assertEqual(
            [event.kind for event in events], ["launched", "outcome"]
        )
        self.assertIsNone(cast(process.ProcessOutcome, events[-1]).error_code)

    def test_argument_values_and_prompt_round_trip_unchanged(self) -> None:
        argv = (sys.executable, "--tools", "", " ", "", "\t")
        prompt = b"synthetic\x00\xff"
        raw = protocol.activation(argv, self.home, {}, prompt, 8192)
        try:
            decoded = protocol.decode_activation(raw, 8192)
        except ValueError as exc:
            self.fail(f"valid argument transport refused: {exc}")
        self.assertEqual(decoded, (argv, self.home, {}, prompt))

    def test_strict_transport_rejections_survive_empty_arguments(self) -> None:
        for argv, cwd, env in (
            ((), self.home, {}),
            (("",), self.home, {}),
            (("relative",), self.home, {}),
            ((sys.executable, "bad\x00"), self.home, {}),
            ((sys.executable,), Path("relative"), {}),
            ((sys.executable,), self.home, {"bad=key": "value"}),
        ):
            with (
                self.subTest(argv=argv, cwd=cwd, env=env),
                self.assertRaises(ValueError),
            ):
                protocol.decode_activation(
                    protocol.activation(argv, cwd, env, b"synthetic", 8192),
                    8192,
                )
        for argv in (
            [sys.executable, 0],
            [sys.executable, False],
            [sys.executable, None],
        ):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                protocol.Control.model_validate_json(
                    json.dumps(
                        {"argv": argv, "cwd": str(self.home), "env": {}}
                    )
                )
