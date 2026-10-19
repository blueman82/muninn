"""Exercise restart refusal and recovery through real disposable SQLite."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import unittest
from collections.abc import Callable
from unittest import mock

from muninn import cli_serve, store
from muninn import review_process as process
from muninn import review_process_identity as native
from muninn import review_supervisor as supervision
from muninn.review_budget import reserve_call
from muninn.review_evidence_models import SourceRef
from muninn.review_models import ProcessIdentity, ReviewContext
from muninn.review_requests import enqueue_review
from muninn.review_runner import LaunchSpec, RunnerCapability
from tests.ingest_support import primary, rollout
from tests.test_classify import user_msg
from tests.test_review_supervisor import SupervisorCase


class ReviewSupervisorRecoveryTests(SupervisorCase):
    """Keep durable cleanup evidence across refusal and concurrent control."""

    def abandoned(self, state: str = "reserved") -> ProcessIdentity:
        """Prepare exact durable boundaries without leaving real children."""
        pending = self.supervisor._claim(13.0)
        assert pending is not None
        with supervision.database(self.home) as conn:
            reserve_call(conn, pending.context, 13.0)
        self.supervisor.stop(14.0)
        previous = ProcessIdentity(
            pid=43210, created="fixture:old", pgid=43210, job_marker=None
        )
        self.conn.execute(
            "UPDATE review_call SET state=?,process_identity_json=?",
            (state, previous.model_dump_json()),
        )
        self.conn.execute(
            "UPDATE review_attempt SET ended_at=NULL,outcome=NULL"
        )
        self.conn.execute(
            "UPDATE review_request SET state='running',"
            "current_attempt_id=(SELECT id FROM review_attempt)"
        )
        return previous

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_repeated_start_refusal_preserves_cleanup_eligibility(
        self,
    ) -> None:
        previous = self.abandoned()
        generation = self.conn.execute(
            "SELECT generation FROM review_control"
        ).fetchone()[0]
        for code in ("process_identity_unverified", "quiescence_unverified"):
            other = supervision.Supervisor(self.home, self.policy)
            with (
                mock.patch.object(
                    native,
                    "identify",
                    return_value=previous.model_copy(
                        update={"created": "other"}
                    ),
                ),
                mock.patch.object(native.os, "killpg") as signal,
                self.assertRaisesRegex(ValueError, code),
            ):
                other.start(15.0)
            signal.assert_not_called()
            self.assertEqual(
                self.conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                generation,
            )
            self.assertEqual(
                self.conn.execute("SELECT state FROM review_call").fetchone()[
                    0
                ],
                "reserved",
            )

    def assert_startup_drift(self, update: str) -> None:
        """Check failed startup without resetting predecessor uncertainty."""
        previous = self.abandoned()

        def signal(pgid: int, number: int) -> None:
            self.assertEqual(pgid, previous.pid)
            with store.writer_lock(self.home, wait_s=0):
                self.conn.execute(update)

        other = supervision.Supervisor(self.home, self.policy)
        with (
            mock.patch.object(native, "identify", return_value=previous),
            mock.patch.object(native.os, "killpg", side_effect=signal),
            self.assertRaisesRegex(
                ValueError, "startup_state_changed|review_policy_required"
            ),
        ):
            other.start(15.0)
        cursor = self.conn.execute("SELECT ended_at FROM review_attempt")
        self.assertIsNone(cursor.fetchone()[0])

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_startup_rechecks_admission_outside_writer_actions(self) -> None:
        self.assert_startup_drift("UPDATE review_control SET admission=0")

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_startup_rechecks_generation_drift(self) -> None:
        self.assert_startup_drift(
            "UPDATE review_control SET generation=generation+1"
        )

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_startup_rechecks_policy_drift(self) -> None:
        self.assert_startup_drift(
            "UPDATE review_control SET policy_digest='changed'"
        )

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_startup_rechecks_call_state_drift(self) -> None:
        self.assert_startup_drift("UPDATE review_call SET state='launched'")

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_startup_rechecks_persisted_identity_drift(self) -> None:
        self.assert_startup_drift(
            "UPDATE review_call SET process_identity_json=NULL"
        )

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_recovery_keeps_completed_call_and_recovers_unfinished_attempt(
        self,
    ) -> None:
        self.abandoned("completed")
        other = supervision.Supervisor(self.home, self.policy)
        with mock.patch.object(native.os, "killpg") as signal:
            other.start(15.0)
        self.addCleanup(other.stop, 20.0)
        signal.assert_not_called()
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_call").fetchone()[0],
            "completed",
        )
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_request").fetchone()[
                0
            ],
            "waiting",
        )

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_successful_signal_does_not_claim_descendant_exit(self) -> None:
        previous = self.abandoned("launched")
        other = supervision.Supervisor(self.home, self.policy)
        with (
            mock.patch.object(native, "identify", return_value=previous),
            mock.patch.object(native.os, "killpg") as signal,
        ):
            other.start(15.0)
        self.addCleanup(other.stop, 20.0)
        signal.assert_called_once()
        row = self.conn.execute("SELECT * FROM review_call").fetchone()
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(
            json.loads(row["process_identity_json"]), previous.model_dump()
        )

    def refusal(
        self, ordinal: int
    ) -> Callable[[ReviewContext, RunnerCapability, bytes], LaunchSpec]:
        """Refuse a fresh host observation at one explicit launch boundary."""
        calls = 0

        def prepare(
            context: ReviewContext, capability: RunnerCapability, prompt: bytes
        ) -> LaunchSpec:
            nonlocal calls
            calls += 1
            if calls == ordinal:
                raise ValueError("effective_host_changed")
            return self.launch(context, capability, prompt)

        return prepare

    def test_fresh_host_refusal_before_creation_never_spawns(self) -> None:
        with (
            mock.patch.object(
                supervision, "prepare_launch", side_effect=self.refusal(2)
            ),
            mock.patch.object(
                supervision.subprocess,
                "Popen",
                side_effect=AssertionError("spawn"),
            ),
        ):
            self.supervisor.tick(13.0)
        self.assertFalse(self.supervisor._rounds)
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_call").fetchone()[0],
            "unknown",
        )

    def test_fresh_host_refusal_before_activation_never_launches_cli(
        self,
    ) -> None:
        with mock.patch.object(
            supervision, "prepare_launch", side_effect=self.refusal(3)
        ):
            self.supervisor.tick(13.0)
            until = time.monotonic() + 5
            while self.supervisor._rounds and time.monotonic() < until:
                self.supervisor.tick(13.0)
                time.sleep(0.01)
        self.assertFalse(self.supervisor._rounds)
        row = self.conn.execute("SELECT * FROM review_call").fetchone()
        self.assertEqual(row["state"], "unknown")
        self.assertIsNotNone(row["process_identity_json"])
        self.assertIsNone(row["launched_at"])

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_missing_creation_identity_refuses_without_signal(self) -> None:
        self.abandoned()
        other = supervision.Supervisor(self.home, self.policy)
        with (
            mock.patch.object(
                native, "identify", side_effect=FileNotFoundError
            ),
            mock.patch.object(native.os, "killpg") as signal,
            self.assertRaisesRegex(ValueError, "process_identity_unverified"),
        ):
            other.start(15.0)
        signal.assert_not_called()

    def test_ingestion_only_owner_never_hot_adopts_later_policy(self) -> None:
        self.supervisor.stop(14.0)
        self.conn.execute("UPDATE review_control SET admission=0")
        poller = cli_serve._Poller(self.home, {}, 60)
        self.addCleanup(poller.close)
        poller._start_review()
        self.assertIsNone(poller.supervisor)
        self.conn.execute("UPDATE review_control SET admission=1")
        with mock.patch.object(
            cli_serve, "Supervisor", side_effect=AssertionError("hot adoption")
        ):
            poller._review_tick()
        second = cli_serve._Poller(self.home, {}, 60)
        self.addCleanup(second.close)
        with self.assertRaisesRegex(ValueError, "supervisor_busy"):
            second._start_review()
        self.assertIsNone(poller.supervisor)

    def test_poller_selects_exact_installed_startup_policy(self) -> None:
        self.supervisor.stop(14.0)
        poller = cli_serve._Poller(self.home, {}, 60)
        self.addCleanup(poller.close)
        with mock.patch.object(
            cli_serve, "Supervisor", wraps=supervision.Supervisor
        ) as factory:
            poller._start_review()
        factory.assert_called_once_with(self.home, self.policy)
        assert poller.supervisor is not None
        self.assertEqual(poller.supervisor.policy, self.policy)

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_ready_and_terminal_resume_retains_outcome_through_busy(
        self,
    ) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX ownership or process APIs")
        self.supervisor.tick(13.0)
        monitor = self.supervisor.monitor
        key = next(iter(self.supervisor._rounds))
        with monitor.lock:
            slot = monitor.slots[key]
        ready: process.WorkerReady | None = None
        terminal: process.ProcessOutcome | None = None
        with store.writer_lock(self.home, wait_s=0):
            until = time.monotonic() + 5
            while time.monotonic() < until:
                with monitor.lock:
                    ready = slot.ready
                if ready is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(ready)
            os.kill(slot.handle.wrapper.pid, signal.SIGKILL)
            while time.monotonic() < until:
                with monitor.lock:
                    terminal = slot.terminal
                if terminal is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(terminal)
            self.assertGreater(slot.handle.deadline, time.monotonic())
        persist = self.supervisor._persist

        def busy_terminal(event: process.ProcessEvent, now: float) -> None:
            if isinstance(event, process.ProcessOutcome):
                raise store.BusyError("busy")
            persist(event, now)

        with mock.patch.object(
            self.supervisor, "_persist", side_effect=busy_terminal
        ):
            self.supervisor.tick(13.0)
            self.assertEqual(len(self.supervisor._rounds), 1)
            self.assertEqual(
                [event.kind for event in self.supervisor._pending], ["outcome"]
            )
            self.assertIs(self.supervisor._pending[0], terminal)
        self.supervisor.tick(13.0)
        self.assertFalse(self.supervisor._rounds)
        self.assertFalse(self.supervisor._pending)
        rows = self.conn.execute("SELECT * FROM review_call").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertIsNone(rows[0]["launched_at"])

    def test_refusal_yields_to_ingest_then_healthy_work_after_restart(
        self,
    ) -> None:
        tid = "0199aaaa-bbbb-4ccc-8ddd-ffffffffffff"
        healthy_path = self.write(rollout(tid), primary(tid))
        self.run_ingest()
        window = self.window().model_copy(
            update={"source": SourceRef(provider="codex", thread_id=tid)}
        )
        healthy = self.transaction(
            lambda: enqueue_review(
                self.conn, window, self.scope, self.policy, "explicit", 12.0
            )
        )
        self.append(healthy_path, [user_msg(3, "new ingest evidence")])
        self.path.unlink()
        poller = cli_serve._Poller(self.home, {}, 60)
        poller.roots, poller.supervisor = self.roots, self.supervisor
        step = poller.step

        def ingest_once() -> None:
            self.assertEqual(
                self.conn.execute(
                    "SELECT state FROM review_request WHERE id=?",
                    (self.request.id,),
                ).fetchone()[0],
                "failed",
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT count(*) FROM review_attempt"
                ).fetchone()[0],
                0,
            )
            step()
            poller.stop_requested = True

        with (
            mock.patch.object(
                poller, "step", side_effect=ingest_once
            ) as ingest,
            mock.patch.object(
                supervision.subprocess,
                "Popen",
                side_effect=AssertionError("spawn before ingest"),
            ),
        ):
            self.assertEqual(poller.run(), (0, None))
        ingest.assert_called_once()
        self.assertEqual(
            self.conn.execute(
                "SELECT cursor_line FROM source WHERE thread_id=?", (tid,)
            ).fetchone()[0],
            4,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            0,
        )
        self.supervisor.stop(14.0)
        self.supervisor = supervision.Supervisor(self.home, self.policy)
        self.supervisor.start(15.0)
        self.addCleanup(self.supervisor.stop, 20.0)
        self.finish()
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM review_receipt WHERE request_id=?",
                (healthy.id,),
            ).fetchone()
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM review_request WHERE id=?",
                (self.request.id,),
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt WHERE request_id=?",
                (self.request.id,),
            ).fetchone()[0],
            0,
        )

    def test_unexpected_claim_errors_propagate_without_mutation(self) -> None:
        for error in (
            ValueError("unknown"),
            OSError("io"),
            supervision.sqlite3.OperationalError("broken"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(
                    supervision, "claim_or_refuse_attempt", side_effect=error
                ),
                self.assertRaises(type(error)),
            ):
                self.supervisor.tick(13.0)
            self.assertEqual(
                self.conn.execute(
                    "SELECT state FROM review_request"
                ).fetchone()[0],
                "pending",
            )
