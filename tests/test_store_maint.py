"""The maintenance opener: lock, absent store and replayed erasure."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import store, store_maint, tombstones
from tests.home_support import initialize, interrupt_erasure


class MaintenanceConnectionTests(unittest.TestCase):
    """Open under the writer lock and finish recorded erasure first."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.home.chmod(0o700)

    def test_absent_store_is_refused(self) -> None:
        with (
            self.assertRaises(store.StoreUnavailableError),
            store_maint.maintenance_connection(self.home, wait_s=0),
        ):
            self.fail("opened an absent store")

    def test_busy_writer_lock_is_refused(self) -> None:
        initialize(self.home)
        with (
            store.writer_lock(self.home, wait_s=0),
            self.assertRaises(store.BusyError),
            store_maint.maintenance_connection(self.home, wait_s=0),
        ):
            self.fail("opened while another writer held the lock")

    def test_recorded_erasure_is_reapplied_once_with_the_lock_held(
        self,
    ) -> None:
        initialize(self.home)
        interrupt_erasure(self.home, "thread")
        reapply = tombstones.reapply_tombstones
        seen: list[bool] = []

        def spy(conn: sqlite3.Connection, home: Path) -> int:
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(home, wait_s=0),
            ):
                self.fail("writer lock not held")
            seen.append(True)
            return reapply(conn, home)

        with (
            mock.patch.object(store_maint, "reapply_tombstones", spy),
            store_maint.maintenance_connection(self.home, wait_s=0) as conn,
        ):
            count = conn.execute("SELECT count(*) FROM tombstone")
            self.assertEqual(count.fetchone()[0], 1)
        self.assertEqual(seen, [True])
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
