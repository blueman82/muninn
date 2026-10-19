"""Check exclusive ownership, retained outcomes and writer boundaries."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast
from unittest import mock

from muninn import cli_serve, platform_lock, review_spawn, store
from muninn import review_process as process
from muninn import review_supervisor as supervision
from muninn.review_models import ReviewContext
from muninn.review_process_identity import identify
from muninn.review_runner import LaunchSpec, RunnerCapability
from tests.review_quiescence_support import initialize
from tests.test_review_requests import RequestCase
from tests.test_review_worker import policy, start_worker

ROOT = Path(__file__).resolve().parent.parent


class SupervisorCase(RequestCase):
    """Exercise real SQLite and local wrappers through main ownership."""

    def setUp(self) -> None:
        super().setUp()
        initialize(self.home)
        self.policy = self.policy.model_copy(
            update={
                "max_input_bytes": 12000,
                "max_output_bytes": 4096,
                "context_bytes": 6000,
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )
        self.request = self.enqueue()
        self.capability = RunnerCapability(
            **self.policy.runner.model_dump(),
            status="verified",
            schema_digest="a" * 64,
            config_digest="b" * 64,
            proof_path=self.home / "fake-proof",
        )
        self.script = (
            "import json; print(json.dumps("
            + repr(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {
                        "op": "final",
                        "result": {
                            "schema_version": 1,
                            "changes": [],
                            "unresolved": [],
                            "no_change_reason": "No supported changes",
                        },
                    },
                }
            )
            + "))"
        )
        for name, value in (
            ("load_capability", self.capability),
            (
                "load_prompt",
                (
                    b'{"schema_version":1,"assembly":"muninn-review-prompt-v1",'
                    b'"instructions":"Review evidence"}'
                ),
            ),
        ):
            patch = mock.patch.object(supervision, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(
            supervision, "prepare_launch", side_effect=self.launch
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.supervisor = supervision.Supervisor(self.home, self.policy)
        self.supervisor.start(12.0)
        self.addCleanup(self.supervisor.stop, 20.0)

    def launch(
        self,
        context: ReviewContext,
        capability: RunnerCapability,
        prompt: bytes,
    ) -> LaunchSpec:
        """Select a disposable fake executable without provider authority."""
        return LaunchSpec(
            (
                sys.executable,
                "-E",
                "-s",
                "-m",
                "muninn.review_worker",
                "--deadline",
                str(context.deadline),
                "--max-input-bytes",
                str(self.policy.max_input_bytes),
                "--max-output-bytes",
                str(self.policy.max_output_bytes),
            ),
            ROOT,
            {},
            (sys.executable, "-c", self.script),
            self.home,
            {},
            prompt,
            context.deadline,
        )

    def finish(self) -> None:
        """Drive main reconciliation until one receipt is durable."""
        until = time.monotonic() + 8
        while time.monotonic() < until:
            self.supervisor.tick(13.0)
            if self.conn.execute("SELECT 1 FROM review_receipt").fetchone():
                return
            time.sleep(0.01)
        self.fail("no receipt")


class ReviewSupervisorTests(SupervisorCase):
    """Check runtime ownership and durable lifecycle coordination."""

    def test_signal_handler_only_sets_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            poller = cli_serve._Poller(Path(directory), {}, 60.0)
            connection = mock.Mock()
            type(connection).in_transaction = mock.PropertyMock(
                side_effect=AssertionError("signal touched SQLite")
            )
            poller.conn = connection
            poller._on_signal(signal.SIGTERM, None)
            self.assertTrue(poller.stop_requested)
            connection.assert_not_called()

    def test_quiescence_requires_supervisor_and_writer_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            owner = platform_lock.acquire(home / "review.supervisor.lock")
            try:
                self.assertFalse(platform_lock.released(home))
            finally:
                os.close(owner)
            with store.writer_lock(home, wait_s=0):
                self.assertFalse(platform_lock.released(home))
            self.assertTrue(platform_lock.released(home))

    def test_second_serve_cannot_fence_live_owner(self) -> None:
        other = supervision.Supervisor(self.home, self.policy)
        generation = self.conn.execute(
            "SELECT generation FROM review_control"
        ).fetchone()[0]
        with self.assertRaisesRegex(ValueError, "supervisor_busy"):
            other.start(13.0)
        self.assertEqual(
            self.conn.execute(
                "SELECT generation FROM review_control"
            ).fetchone()[0],
            generation,
        )

    def test_main_never_spawns_wrapper_while_owning_writer_lock(self) -> None:
        real = subprocess.Popen

        def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            self.assertIs(threading.current_thread(), threading.main_thread())
            with store.writer_lock(self.home, wait_s=0):
                self.assertEqual(
                    self.conn.execute(
                        "SELECT state FROM review_call"
                    ).fetchone()[0],
                    "reserved",
                )
            self.assertTrue(kwargs["close_fds"])
            return cast(subprocess.Popen[bytes], real(*args, **kwargs))

        with mock.patch.object(
            review_spawn.subprocess, "Popen", side_effect=spawn
        ):
            self.finish()
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_request").fetchone()[
                0
            ],
            "completed",
        )

    def test_queue_saturation_cannot_drop_terminal_outcome(self) -> None:
        self.supervisor.tick(13.0)
        persist = self.supervisor._persist

        def block_outcome(event: process.ProcessEvent, now: float) -> None:
            if isinstance(event, process.ProcessOutcome):
                raise store.BusyError("busy")
            persist(event, now)

        until = time.monotonic() + 5
        with mock.patch.object(
            self.supervisor, "_persist", side_effect=block_outcome
        ):
            while time.monotonic() < until and not any(
                isinstance(event, process.ProcessOutcome)
                for event in self.supervisor._pending
            ):
                self.supervisor.tick(13.0)
                time.sleep(0.01)
            self.assertEqual(len(self.supervisor._rounds), 1)
            self.assertTrue(
                any(
                    isinstance(event, process.ProcessOutcome)
                    for event in self.supervisor._pending
                )
            )
        self.finish()
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_deadline_expires_during_blocked_ingest(self) -> None:
        deadline = time.monotonic() + 0.3
        child = start_worker(deadline)
        monitor = process.ProcessMonitor(policy())
        self.addCleanup(monitor.stop_all)
        monitor.register(
            process.RunningProcess(
                "A_" + "a" * 32, 1, child, identify(child.pid), deadline, 4096
            )
        )
        with store.writer_lock(self.home, wait_s=0):
            time.sleep(0.5)
            self.assertIsNotNone(child.returncode)
            events = monitor.drain()
            self.assertIsInstance(events[-1], process.ProcessOutcome)
            self.assertEqual(
                cast(process.ProcessOutcome, events[-1]).error_code, "deadline"
            )

    def test_monitor_has_no_sqlite_or_spawn(self) -> None:
        deadline = time.monotonic() + 0.3
        child = start_worker(deadline)
        monitor = process.ProcessMonitor(policy())
        self.addCleanup(monitor.stop_all)
        monitor.register(
            process.RunningProcess(
                "A_" + "a" * 32, 1, child, identify(child.pid), deadline, 4096
            )
        )
        with (
            mock.patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("monitor spawn"),
            ),
            mock.patch.object(
                supervision.sqlite3,
                "connect",
                side_effect=AssertionError("monitor SQLite"),
            ),
        ):
            time.sleep(0.5)
            self.assertIsNotNone(child.returncode)
            self.assertEqual(monitor.drain()[-1].kind, "outcome")

    def test_writer_contention_during_preparation_retains_pending_permit(
        self,
    ) -> None:
        real = supervision.review_prompt.initialize
        with mock.patch.object(
            supervision.review_prompt,
            "initialize",
            side_effect=store.BusyError("busy"),
        ):
            self.supervisor.tick(13.0)
            self.assertEqual(len(self.supervisor._rounds), 1)
        with mock.patch.object(
            supervision.review_prompt, "initialize", side_effect=real
        ):
            self.finish()
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )

    def test_registration_failure_cleans_adopted_wrapper(self) -> None:
        real = self.supervisor.monitor.register

        def fail_after_adopt(handle: process.RunningProcess) -> None:
            real(handle)
            raise ValueError("injected registration failure")

        with mock.patch.object(
            self.supervisor.monitor, "register", side_effect=fail_after_adopt
        ):
            self.supervisor.tick(13.0)
        time.sleep(0.15)
        self.supervisor.tick(13.0)
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_call").fetchone()[0],
            "unknown",
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT launched_at FROM review_call"
            ).fetchone()[0]
        )

    def test_partial_registration_failure_retains_cleanup_and_permit(
        self,
    ) -> None:
        with mock.patch.object(
            process.os, "set_blocking", side_effect=RuntimeError("injected")
        ):
            self.supervisor.tick(13.0)
        slot = next(iter(self.supervisor.monitor.slots.values()))
        try:
            time.sleep(0.2)
            with mock.patch.object(
                self.supervisor,
                "_persist",
                side_effect=store.BusyError("busy"),
            ):
                self.supervisor.tick(13.0)
                self.assertIsNotNone(slot.handle.wrapper.returncode)
                self.assertEqual(len(self.supervisor._rounds), 1)
                self.assertEqual(
                    [event.kind for event in self.supervisor._pending],
                    ["outcome"],
                )
        finally:
            slot.configured = True
        self.supervisor.tick(13.0)
        self.assertFalse(self.supervisor._rounds)
        self.assertIsNone(
            self.conn.execute(
                "SELECT launched_at FROM review_call"
            ).fetchone()[0]
        )

    def test_identity_persistence_failure_never_activates(self) -> None:
        self.supervisor.tick(13.0)
        time.sleep(0.15)
        with mock.patch.object(
            self.supervisor, "_persist", side_effect=store.BusyError("busy")
        ):
            self.supervisor.tick(13.0)
        self.assertIsNone(
            self.conn.execute(
                "SELECT process_identity_json FROM review_call"
            ).fetchone()[0]
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT launched_at FROM review_call"
            ).fetchone()[0]
        )
        self.finish()

    def test_duplicate_round_cannot_create_another_wrapper(self) -> None:
        self.supervisor.tick(13.0)
        pending = next(iter(self.supervisor._rounds.values()))
        with (
            mock.patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("duplicate spawn"),
            ),
            self.assertRaisesRegex(ValueError, "duplicate_round"),
        ):
            self.supervisor._spawn(pending, 13.0)

    def test_restart_after_each_persistence_boundary(self) -> None:
        self.supervisor.tick(13.0)
        self.supervisor.stop(14.0)
        other = supervision.Supervisor(self.home, self.policy)
        other.start(15.0)
        self.addCleanup(other.stop, 20.0)
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_call").fetchone()[0],
            "unknown",
        )
        self.assertNotEqual(
            self.conn.execute("SELECT state FROM review_request").fetchone()[
                0
            ],
            "cancelled",
        )

    def test_sqlite_contention_codes_preserve_pending_claim(self) -> None:
        for code in (
            supervision.sqlite3.SQLITE_BUSY,
            supervision.sqlite3.SQLITE_LOCKED,
            supervision.sqlite3.SQLITE_BUSY_SNAPSHOT,
        ):
            error = supervision.sqlite3.OperationalError("fixture")
            error.sqlite_errorcode = code
            with mock.patch.object(
                supervision, "claim_or_refuse_attempt", side_effect=error
            ):
                self.supervisor.tick(13.0)
            self.assertEqual(
                self.conn.execute(
                    "SELECT state FROM review_request"
                ).fetchone()[0],
                "pending",
            )
            self.assertFalse(self.supervisor._rounds)
