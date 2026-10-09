"""Concurrent schema migrations: real connections copy each version once."""

from __future__ import annotations

import json
import sqlite3
import threading
from unittest import mock

from muninn import store
from tests.store_support import StoreCase
from tests.test_store_migrate import make_v1


class MigrationRaceTests(StoreCase):
    """Concurrent openers preserve rows and migrate each transition once."""

    def test_two_real_connections_racing_migrate_once(self) -> None:
        make_v1(self.db)
        gate = threading.Barrier(2, timeout=10)
        real = store.migrate_v1_to_v2
        real_v2 = store.migrate_v2_to_v3
        v1_results: list[int | None] = []
        v2_results: list[bool] = []

        def after_both_saw_v1(conn: sqlite3.Connection) -> int | None:
            gate.wait()  # neither starts before both read user_version 1
            result = real(conn)
            v1_results.append(result)
            return result

        def migrate_v2(conn: sqlite3.Connection) -> bool:
            result = real_v2(conn)
            v2_results.append(result)
            return result

        failures: list[BaseException] = []

        def open_store() -> None:
            try:
                store.connect_rw(self.db, fullfsync=False).close()
            except BaseException as exc:  # reported on the main thread
                failures.append(exc)

        with (
            mock.patch.object(store, "migrate_v1_to_v2", after_both_saw_v1),
            mock.patch.object(store, "migrate_v2_to_v3", migrate_v2),
        ):
            workers = [threading.Thread(target=open_store) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
        self.assertEqual(failures, [])
        self.assertCountEqual(v1_results, [4, None])
        self.assertEqual(v2_results.count(True), 1)
        text = (self.db.parent / "poller.log").read_text()
        events = sorted(json.loads(x)["event"] for x in text.splitlines())
        # Separate connections can win the two schema transitions, in which
        # case both correctly report that they performed a migration.
        self.assertIn(
            events,
            (["migrate_raced", "migrated"], ["migrated", "migrated"]),
        )
        conn = self.rw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM knowledge").fetchone()[0], 4
        )
