"""Exercise retained maintenance ownership in disposable current stores."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from muninn import review_quiescence as q
from muninn import review_quiescence_state as state
from muninn import store
from tests.review_quiescence_support import interrupt_erasure
from tests.store_support import Child
from tests.test_review_quiescence import QuiescenceCase


class MaintenanceTests(QuiescenceCase):
    """Separate lifecycle exclusion from containment and source identity."""

    def owned(
        self, wait_s: float = 0
    ) -> contextlib.AbstractContextManager[q.MaintenanceOwner]:
        """Retain the real owner with this fixture's caller-owned status."""
        return q.maintenance_owner(
            self.home, wait_s=wait_s, status=self.status
        )

    @contextlib.contextmanager
    def connected(
        self, wait_s: float = 0
    ) -> Generator[tuple[q.MaintenanceOwner, sqlite3.Connection]]:
        """Open and close a current connection inside both real barriers."""
        with (
            self.owned(wait_s) as owner,
            q.maintenance_writer(owner),
            contextlib.closing(
                store.connect_rw(store.db_path(self.home))
            ) as conn,
        ):
            yield owner, conn

    def test_owner_retained_across_sequential_writer_regions(self) -> None:
        before = (self.home / state.WITNESS).read_bytes()
        with self.owned(0) as owner:
            self.assertIs(owner.status, self.status)
            for _ in range(2):
                with self.assertRaises(BlockingIOError):
                    state.acquire(self.home)
                with store.writer_lock(self.home, wait_s=0):
                    pass
                with (
                    q.maintenance_writer(owner),
                    contextlib.closing(
                        store.connect_rw(store.db_path(self.home))
                    ) as conn,
                ):
                    q.verify_current(owner, conn)
                    with (
                        self.assertRaises(store.BusyError),
                        store.writer_lock(self.home, wait_s=0),
                    ):
                        self.fail("writer released")
                    self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
        os.close(state.acquire(self.home))

    def test_contexts_neither_open_store_nor_seed_missing_witness(
        self,
    ) -> None:
        store.db_path(self.home).unlink()
        (self.home / state.WITNESS).unlink()
        with (
            mock.patch.object(
                store, "connect_rw", side_effect=AssertionError("opened DB")
            ),
            self.owned(0) as owner,
            q.maintenance_writer(owner),
        ):
            self.assertFalse(store.db_path(self.home).exists())
            self.assertFalse((self.home / state.WITNESS).exists())

    def test_nested_owner_and_writer_refuse_without_releasing_outer(
        self,
    ) -> None:
        with self.owned(0) as owner:
            with (
                self.assertRaisesRegex(ValueError, "coordination_timeout"),
                self.owned(0),
            ):
                self.fail("nested owner")
            with q.maintenance_writer(owner):
                with (
                    self.assertRaises(RuntimeError),
                    q.maintenance_writer(owner),
                ):
                    self.fail("nested writer")
                with self.assertRaises(BlockingIOError):
                    state.acquire(self.home)

    def test_closed_and_outside_writer_uses_refuse(self) -> None:
        with (
            self.owned(0) as owner,
            contextlib.closing(sqlite3.connect(":memory:")) as conn,
            self.assertRaises(RuntimeError),
        ):
            q.verify_current(owner, conn)
        with (
            self.assertRaises(RuntimeError),
            q.maintenance_writer(owner),
        ):
            self.fail("closed owner")
        with (
            contextlib.closing(sqlite3.connect(":memory:")) as conn,
            self.assertRaises(RuntimeError),
        ):
            q.verify_current(owner, conn)

    def test_writer_acquisition_is_not_a_held_writer_scope(self) -> None:
        with (
            self.owned() as owner,
            contextlib.closing(sqlite3.connect(":memory:")) as conn,
        ):

            def acquiring(
                home: Path, deadline: float, resources: contextlib.ExitStack
            ) -> None:
                with self.assertRaises(RuntimeError):
                    q.verify_current(owner, conn)
                raise store.BusyError("not acquired")

            with (
                mock.patch.object(q, "_writer", side_effect=acquiring),
                self.assertRaises(store.BusyError),
                q.maintenance_writer(owner),
            ):
                self.fail("writer acquired")

    def test_owner_exit_with_open_writer_refuses_and_releases_in_order(
        self,
    ) -> None:
        root = self.owned(0)
        owner = root.__enter__()
        writer = q.maintenance_writer(owner)
        writer.__enter__()
        with self.assertRaises(RuntimeError):
            root.__exit__(None, None, None)
        writer.__exit__(None, None, None)
        os.close(state.acquire(self.home))
        with store.writer_lock(self.home, wait_s=0):
            pass

    def test_replaced_owner_lock_refuses_next_writer_region(self) -> None:
        with self.owned(0) as owner:
            (self.home / state.LOCK).rename(self.home / "old-lock")
            os.close(state.acquire(self.home))
            with (
                self.assertRaisesRegex(ValueError, "quiescence_unverified"),
                q.maintenance_writer(owner),
            ):
                self.fail("replaced lock")

    def test_wait_validation_and_all_entrypoints_require_main_thread(
        self,
    ) -> None:
        for value in (-1, float("nan"), float("inf")):
            with (
                self.assertRaisesRegex(ValueError, "invalid_wait"),
                self.owned(value),
            ):
                self.fail("invalid wait")
        errors: list[str] = []

        def other_thread(
            owner: q.MaintenanceOwner, conn: sqlite3.Connection
        ) -> None:
            operations = (
                lambda: self.owned(0).__enter__(),
                lambda: q.maintenance_writer(owner).__enter__(),
                lambda: q.verify_current(owner, conn),
            )
            for operation in operations:
                try:
                    operation()
                except RuntimeError as exc:
                    errors.append(str(exc))

        with self.connected() as (owner, conn):
            worker = threading.Thread(target=other_thread, args=(owner, conn))
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["main_thread_required"] * 3)

    def test_existing_contender_cannot_acquire_either_barrier(self) -> None:
        child = Child(
            self,
            """
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from muninn import platform_lock
home = Path(sys.argv[2])
print("ready", flush=True)
deadline = time.monotonic() + 10
while not (home / "go").exists():
    if time.monotonic() > deadline:
        raise RuntimeError("trigger missing")
    time.sleep(0.01)
for name in ("review.supervisor.lock", "writer.lock"):
    try:
        fd = platform_lock.acquire(home / name)
    except BlockingIOError:
        print(name, flush=True)
    else:
        os.close(fd)
        raise RuntimeError("barrier acquired")
""",
            self.home,
        )
        child.wait_ready(timeout=5)
        with (
            self.owned(0) as owner,
            q.maintenance_writer(owner),
        ):
            (self.home / "go").touch()
            out, err = child.proc.communicate(timeout=5)
        self.assertEqual(
            (child.proc.returncode, out, err),
            (0, "review.supervisor.lock\nwriter.lock\n", ""),
        )

    def test_shared_deadline_selected_stop_and_replacement_refusal(
        self,
    ) -> None:
        self.selected_stop(False)
        self.selected_stop(True)

    def selected_stop(self, replace: bool) -> None:
        """Exercise one selected predecessor with a shared remaining budget."""
        fd, token = state.begin_owner(self.home)
        elapsed = [0.0]
        targets: list[str] = []

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            self.assertIs(self.status.stop_may_have_been_published, True)
            self.assertEqual((pid, deadline), (os.getpid(), 0.25))
            targets.append(generation)
            elapsed[0] = 0.15
            state.complete_owner(home, fd, token)
            os.close(fd)
            if replace:
                successor, successor_token = state.begin_owner(home)
                state.complete_owner(home, successor, successor_token)
                os.close(successor)

        def sleep(seconds: float) -> None:
            elapsed[0] += seconds

        clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep)
        with (
            mock.patch.object(q, "time", clock),
            mock.patch.object(q.poller_stop, "request", side_effect=stop),
        ):
            if replace:
                with (
                    self.assertRaisesRegex(ValueError, "owner_changed"),
                    self.connected(0.25) as (owner, conn),
                ):
                    q.verify_current(owner, conn)
            else:
                with (
                    store.writer_lock(self.home, wait_s=0),
                    self.owned(0.25) as owner,
                    self.assertRaises(store.BusyError),
                    q.maintenance_writer(owner),
                ):
                    self.fail("busy writer")
                self.assertAlmostEqual(elapsed[0], 0.25)
        self.assertEqual(targets, [token])

    def test_replay_precedes_retirement_and_preserves_caller_connection(
        self,
    ) -> None:
        before = interrupt_erasure(self.home, "maintenance-thread")
        retire = state.retire_baseline
        with self.connected() as (owner, conn):

            def checked(home: Path, fd: int) -> None:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM tombstone").fetchone()[
                        0
                    ],
                    1,
                )
                retire(home, fd)

            with mock.patch.object(
                state, "retire_baseline", side_effect=checked
            ):
                q.verify_current(owner, conn)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
        self.assertEqual(self.status, q.EraseStatus())

    def test_wrong_home_memory_and_unmarked_connections_refuse(self) -> None:
        foreign = self.home / "other"
        foreign.mkdir()
        with (
            self.owned(0) as owner,
            q.maintenance_writer(owner),
        ):
            for path in (":memory:", str(foreign / "muninn.sqlite")):
                with (
                    contextlib.closing(
                        sqlite3.connect(path, isolation_level=None)
                    ) as conn,
                    self.assertRaises(ValueError),
                ):
                    q.verify_current(owner, conn)
            with contextlib.closing(
                store.connect_rw(store.db_path(self.home))
            ) as conn:
                conn.execute("PRAGMA user_version=3")
                with self.assertRaises(store.StoreUnavailableError):
                    q.verify_current(owner, conn)

    def test_transactions_refuse_without_committing_caller_work(self) -> None:
        with self.connected() as (owner, conn):
            conn.isolation_level = "DEFERRED"
            with self.assertRaises(ValueError):
                q.verify_current(owner, conn)
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE review_control SET generation=42")
            with self.assertRaises(ValueError):
                q.verify_current(owner, conn)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            self.assertEqual(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                0,
            )

    def test_uncertain_or_baseline_maintenance_never_becomes_repair(
        self,
    ) -> None:
        for mode in ("baseline", "active", "missing"):
            if mode == "baseline":
                store.write_json_atomic(
                    self.home / state.BASELINE, {"old": True}
                )
            elif mode == "active":
                (self.home / state.BASELINE).unlink()
                self.abandon()
            else:
                (self.home / state.WITNESS).unlink()
            with (
                mock.patch.object(
                    q.review_boot_identity,
                    "observe",
                    side_effect=AssertionError("repair"),
                ),
                self.connected(1) as (owner, conn),
                self.assertRaisesRegex(ValueError, "quiescence_unverified"),
            ):
                q.verify_current(owner, conn)
            self.assertEqual(self.status.repair_phase, "none")
            self.assertEqual(
                (self.home / state.BASELINE).exists(), mode == "baseline"
            )

    def test_replay_error_keeps_witness_status_and_original_exception(
        self,
    ) -> None:
        before = interrupt_erasure(self.home, "maintenance-thread")
        original = LookupError("replay failed")
        with self.connected() as (owner, conn):
            with (
                mock.patch.object(
                    q, "reapply_tombstones", side_effect=original
                ),
                self.assertRaises(LookupError) as caught,
            ):
                q.verify_current(owner, conn)
            self.assertIs(caught.exception, original)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0], 0
            )
            q.verify_current(owner, conn)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
        self.assertEqual(self.status, q.EraseStatus())
