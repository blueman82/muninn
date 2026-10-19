"""Exercise foreground barriers and recurrent repair in disposable homes."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from muninn import review_quiescence as quiescence
from muninn import review_quiescence_state as state
from muninn import store
from tests.review_quiescence_support import initialize, interrupt_erasure

BOOT_A = "linux:12345678-1234-4234-8234-123456789abc"
BOOT_B = "linux:87654321-1234-4234-8234-123456789abc"


class QuiescenceCase(unittest.TestCase):
    """Keep owner and writer authority through caller-owned erasure work."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        initialize(self.home)
        with store.writer_lock(self.home, wait_s=0):
            store.connect_rw(store.db_path(self.home)).close()
        self.status = quiescence.EraseStatus()

    def abandon(self) -> bytes:
        """Leave an uncertain active predecessor without pretending cleanup."""
        fd, _ = state.begin_owner(self.home)
        os.close(fd)
        return (self.home / state.WITNESS).read_bytes()

    def refuse(self, boot: str = BOOT_A) -> None:
        """Record or reuse a baseline without consuming a fresh boot."""
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=boot
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("uncertain predecessor admitted")


class QuiescenceTests(QuiescenceCase):
    """Keep acquisition bounded and preserve original failure reporting."""

    def test_startup_close_failure_preserves_original_and_blocks_active(
        self,
    ) -> None:
        before = interrupt_erasure(self.home, "fixture-thread")
        connect = store.connect_rw
        replay = state.reapply_tombstones
        original = LookupError("replay")
        cleanup = OSError("close")

        def failing_close(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            proxy = mock.Mock(wraps=conn)
            type(proxy).in_transaction = mock.PropertyMock(
                side_effect=lambda: conn.in_transaction
            )

            def close() -> None:
                conn.close()
                raise cleanup

            proxy.close.side_effect = close
            return cast(sqlite3.Connection, proxy)

        for action, error in ((original, original), (replay, cleanup)):
            with (
                mock.patch.object(
                    store, "connect_rw", side_effect=failing_close
                ),
                mock.patch.object(
                    state, "reapply_tombstones", side_effect=action
                ),
                self.assertRaises(type(error)) as caught,
            ):
                state.begin_owner(self.home)
            self.assertIs(caught.exception, error)
            self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
        fd, token = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        state.complete_owner(self.home, fd, token)

    def test_complete_owner_yields_autocommit_under_both_barriers(
        self,
    ) -> None:
        with quiescence.erase_connection(
            self.home, wait_s=0, status=self.status
        ) as conn:
            self.assertFalse(conn.in_transaction)
            with self.assertRaises(BlockingIOError):
                state.acquire(self.home)
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.home, wait_s=0),
            ):
                self.fail("nested writer acquired")
            conn.execute("CREATE TABLE caller_work(value INTEGER)")
        fd = state.acquire(self.home)
        os.close(fd)
        self.assertEqual(self.status, quiescence.EraseStatus())

    def test_zero_wait_preserves_actual_writer_busy_without_side_effects(
        self,
    ) -> None:
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(quiescence.poller_stop, "request") as request,
            mock.patch.object(quiescence.time, "sleep") as sleep,
            self.assertRaises(store.BusyError),
            quiescence.erase_connection(
                self.home, wait_s=0, status=self.status
            ),
        ):
            self.fail("writer acquired")
        request.assert_not_called()
        sleep.assert_not_called()
        self.assertFalse((self.home / state.BASELINE).exists())

    def test_zero_wait_with_busy_owner_never_requests_stop(self) -> None:
        fd, _ = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        with (
            mock.patch.object(quiescence.poller_stop, "request") as request,
            mock.patch.object(quiescence.time, "sleep") as sleep,
            self.assertRaisesRegex(ValueError, "coordination_timeout"),
            quiescence.erase_connection(
                self.home, wait_s=0, status=self.status
            ),
        ):
            self.fail("owner acquired")
        request.assert_not_called()
        sleep.assert_not_called()
        self.assertFalse(self.status.stop_may_have_been_published)

    def test_zero_wait_uncertain_owner_never_records_baseline(self) -> None:
        self.abandon()
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe"
            ) as boot,
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
            quiescence.erase_connection(
                self.home, wait_s=0, status=self.status
            ),
        ):
            self.fail("uncertain owner admitted")
        boot.assert_not_called()
        self.assertFalse((self.home / state.BASELINE).exists())

    def test_same_boot_refuses_then_later_boot_reconciles_and_retires(
        self,
    ) -> None:
        original = self.abandon()
        self.refuse()
        baseline = (self.home / state.BASELINE).read_bytes()
        self.refuse()
        self.assertEqual((self.home / state.WITNESS).read_bytes(), original)
        self.assertEqual((self.home / state.BASELINE).read_bytes(), baseline)
        self.assertEqual(self.status.repair_phase, "baseline_recorded")
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ) as conn,
        ):
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(self.status.repair_phase, "complete")
            self.assertEqual(self.status.erasure_phase, "not_started")
            self.assertFalse((self.home / state.BASELINE).exists())
        witness = json.loads((self.home / state.WITNESS).read_bytes())
        self.assertEqual(
            (witness["state"], witness["owner"]), ("complete", None)
        )

    def test_changed_predecessor_or_baseline_requires_another_boot(
        self,
    ) -> None:
        self.abandon()
        self.refuse()
        path = self.home / state.WITNESS
        path.write_bytes(path.read_bytes() + b" ")
        self.refuse(BOOT_B)
        self.assertEqual(
            json.loads((self.home / state.BASELINE).read_bytes())["boot"],
            BOOT_B,
        )
        (self.home / state.BASELINE).unlink()
        self.refuse(BOOT_B)

    def test_unsupported_boot_never_creates_repair_authority(self) -> None:
        original = self.abandon()
        with (
            mock.patch.object(
                quiescence.review_boot_identity,
                "observe",
                side_effect=state.QuiescenceRefusedError(
                    "boot_identity_unavailable"
                ),
            ),
            self.assertRaisesRegex(ValueError, "boot_identity_unavailable"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("unsupported repair admitted")
        self.assertFalse((self.home / state.BASELINE).exists())
        self.assertEqual((self.home / state.WITNESS).read_bytes(), original)

    def test_no_store_does_not_initialize_or_record_baseline(self) -> None:
        store.db_path(self.home).unlink()
        before = (self.home / state.WITNESS).read_bytes()
        with (
            self.assertRaises(store.StoreUnavailableError),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("missing store created")
        with self.assertRaises(store.StoreUnavailableError):
            state.begin_owner(self.home)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
        self.assertFalse(store.db_path(self.home).exists())
        self.assertFalse((self.home / state.BASELINE).exists())

    def test_stop_target_is_selected_once_and_completion_allows_entry(
        self,
    ) -> None:
        fd, token = state.begin_owner(self.home)

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            self.assertTrue(self.status.stop_may_have_been_published)
            self.assertEqual((pid, generation), (os.getpid(), token))
            state.complete_owner(home, fd, token)
            os.close(fd)

        with (
            mock.patch.object(
                quiescence.poller_stop, "request", side_effect=stop
            ) as request,
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.assertEqual(request.call_count, 1)

    def test_replacement_owner_is_refused_without_chasing_it(self) -> None:
        fd, token = state.begin_owner(self.home)

        def replace(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            state.complete_owner(home, fd, token)
            os.close(fd)
            replacement, new_token = state.begin_owner(home)
            state.complete_owner(home, replacement, new_token)
            os.close(replacement)

        with (
            mock.patch.object(
                quiescence.poller_stop, "request", side_effect=replace
            ) as request,
            self.assertRaisesRegex(ValueError, "owner_changed"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("replacement admitted")
        self.assertEqual(request.call_count, 1)

    def test_publication_failure_keeps_possible_stop_and_original_exception(
        self,
    ) -> None:
        fd, _ = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        error = OSError("publisher failed after visibility")
        with (
            mock.patch.object(
                quiescence.poller_stop, "request", side_effect=error
            ),
            self.assertRaises(OSError) as caught,
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("failed publication admitted")
        self.assertIs(caught.exception, error)
        self.assertTrue(self.status.stop_may_have_been_published)
        self.assertEqual(self.status.erasure_phase, "not_started")

    def test_one_deadline_bounds_programmed_owner_acquisition_sleeps(
        self,
    ) -> None:
        fd, _ = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        elapsed = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            elapsed[0] += seconds

        clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep)
        with (
            mock.patch.object(quiescence, "time", clock),
            mock.patch.object(quiescence.poller_stop, "request") as request,
            self.assertRaisesRegex(ValueError, "coordination_timeout"),
            quiescence.erase_connection(
                self.home, wait_s=0.25, status=self.status
            ),
        ):
            self.fail("busy owner acquired")
        self.assertAlmostEqual(sum(sleeps), 0.25)
        self.assertEqual(request.call_count, 1)

    def test_writer_wait_uses_remaining_owner_budget(self) -> None:
        fd, token = state.begin_owner(self.home)
        elapsed = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            elapsed[0] += seconds

        def stop(
            home: Path, pid: int, generation: str, *, deadline: float
        ) -> None:
            elapsed[0] = 0.15
            state.complete_owner(home, fd, token)
            os.close(fd)

        clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep)
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(quiescence, "time", clock),
            mock.patch.object(
                quiescence.poller_stop, "request", side_effect=stop
            ),
            self.assertRaises(store.BusyError),
            quiescence.erase_connection(
                self.home, wait_s=0.25, status=self.status
            ),
        ):
            self.fail("busy writer acquired")
        self.assertAlmostEqual(sum(sleeps), 0.1)
        self.assertAlmostEqual(elapsed[0], 0.25)

    def test_staging_expiry_keeps_conservative_stop_status(self) -> None:
        fd, _ = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        elapsed = [0.0]

        def sync(descriptor: int) -> None:
            elapsed[0] = 1.0

        clock = SimpleNamespace(monotonic=lambda: elapsed[0])
        with (
            mock.patch.object(quiescence, "time", clock),
            mock.patch.object(quiescence.poller_stop, "time", clock),
            mock.patch.object(
                quiescence.poller_stop.file_sync, "sync_fd", side_effect=sync
            ),
            self.assertRaisesRegex(ValueError, "coordination_timeout"),
            quiescence.erase_connection(
                self.home, wait_s=0.5, status=self.status
            ),
        ):
            self.fail("expired stop publication admitted")
        self.assertTrue(self.status.stop_may_have_been_published)
        self.assertFalse((self.home / quiescence.poller_stop.FILE).exists())
