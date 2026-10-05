"""Schema v1 to v2 migration: ids, chains, FTS, erased rows, crash safety."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import store
from muninn.store_migrate import migrate_v1_to_v2
from tests.store_support import StoreCase
from tests.store_v1_schema import V1_SCHEMA_SQL


def make_v1(db: Path) -> None:
    """Write a v1 ledger: a chain K1<-K2, a retracted K3 and an erased K4."""
    store.ensure_private_dir(db.parent)
    raw = sqlite3.connect(db, isolation_level=None)
    raw.executescript(V1_SCHEMA_SQL)
    raw.executescript("""
INSERT INTO scope(id, key, label, kind) VALUES (1, '/repo', 'repo', 'git');
INSERT INTO knowledge(id, scope_id, kind, text, status, supersedes,
  superseded_by, retract_reason, actor, created_at) VALUES
 (1, 1, 'decision', 'use sqlite always', 'superseded', NULL, 2, NULL,
  'user', 1),
 (2, 1, 'decision', 'use sqlite wal never', 'current', 1, NULL, NULL,
  'user', 2),
 (3, 1, 'fact', 'moon is cheese', 'retracted', NULL, NULL, 'wrong',
  'user', 3),
 (4, 1, 'fact', NULL, 'erased', NULL, NULL, NULL, 'user', 4);
INSERT INTO citation(knowledge_id, provider, thread_id, line, part,
  line_sha256, role, kind, quote) VALUES
 (2, 'claude', 't', 1, 1, 'h', 'user', 'prompt', 'use sqlite wal never');
INSERT INTO knowledge_log(knowledge_id, action, actor, at) VALUES
 (1, 'add', 'user', 1), (2, 'add', 'user', 2);
PRAGMA user_version=1;
""")
    raw.close()


def rows(conn: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    """Return ``sql`` results as plain tuples."""
    return [tuple(r) for r in conn.execute(sql)]


class MigrateTests(StoreCase):
    """connect_rw upgrades a v1 file once and keeps the ledger intact."""

    def test_v1_ledger_survives_with_ids_chain_citation_and_fts(self) -> None:
        make_v1(self.db)
        conn = self.rw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(
            rows(conn, "SELECT id, status, supersedes FROM knowledge"),
            [
                (1, "superseded", None),
                (2, "current", 1),
                (3, "retracted", None),
                (4, "erased", None),
            ],
        )
        self.assertEqual(
            rows(conn, "SELECT superseded_by FROM knowledge WHERE id = 1"),
            [(2,)],
        )
        self.assertEqual(
            rows(
                conn,
                "SELECT confidence, valid_until, sensitivity, contradicts,"
                " tags FROM knowledge WHERE id = 2",
            ),
            [(None, None, "normal", None, None)],
        )
        self.assertEqual(
            rows(conn, "SELECT knowledge_id FROM citation"), [(2,)]
        )
        self.assertEqual(
            rows(conn, "SELECT count(*) FROM knowledge_log"), [(2,)]
        )
        hits = rows(
            conn,
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'wal'",
        )
        self.assertEqual(hits, [(2,)])
        self.assertEqual(rows(conn, "PRAGMA foreign_key_check"), [])
        self.assertEqual(rows(conn, "PRAGMA integrity_check"), [("ok",)])
        conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (1, 'lesson', 'new term', 'current', 'u', 5)"
        )
        self.assertEqual(
            rows(
                conn,
                "SELECT rowid FROM knowledge_fts"
                " WHERE knowledge_fts MATCH 'term'",
            ),
            [(5,)],
        )

    def test_erased_row_stays_out_of_fts_after_a_later_update(self) -> None:
        make_v1(self.db)
        conn = self.rw()
        conn.execute("UPDATE knowledge SET actor = 'u2' WHERE id = 4")
        conn.execute("UPDATE knowledge SET actor = 'u2' WHERE id = 2")
        self.assertEqual(
            rows(
                conn,
                "INSERT INTO knowledge_fts(knowledge_fts) VALUES"
                " ('integrity-check')",
            ),
            [],
        )

    def test_rerun_is_idempotent(self) -> None:
        make_v1(self.db)
        conn = self.rw()
        before = rows(conn, "SELECT * FROM knowledge")
        migrate_v1_to_v2(conn)
        self.assertEqual(rows(conn, "SELECT * FROM knowledge"), before)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_foreign_keys_setting_is_restored(self) -> None:
        make_v1(self.db)
        conn = self.rw()
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_old_kind_rejected_check_widened(self) -> None:
        make_v1(self.db)
        conn = self.rw()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
                " created_at) VALUES (1, 'bogus', 'x', 'current', 'u', 9)"
            )

    def test_crash_before_commit_leaves_a_clean_v1_file(self) -> None:
        make_v1(self.db)

        class Crashing(sqlite3.Connection):
            """Dies at the user_version bump, the last step before COMMIT."""

            def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
                if sql == "PRAGMA user_version=2":
                    raise sqlite3.OperationalError("injected crash")
                return super().execute(sql, *args)

        conn = sqlite3.connect(self.db, isolation_level=None, factory=Crashing)
        with self.assertRaises(sqlite3.OperationalError):
            migrate_v1_to_v2(conn)
        conn.close()
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 1)
        cols = [r[1] for r in raw.execute("PRAGMA table_info(knowledge)")]
        self.assertNotIn("tags", cols)
        self.assertEqual(rows(raw, "SELECT count(*) FROM knowledge"), [(4,)])
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("knowledge_v2", names)
        # and the next open still migrates cleanly
        self.assertEqual(
            self.rw().execute("PRAGMA user_version").fetchone()[0], 2
        )

    def test_open_logs_one_migration_line(self) -> None:
        make_v1(self.db)
        self.rw()
        lines = (self.db.parent / "poller.log").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        got = json.loads(lines[0])
        self.assertEqual(
            (got["event"], got["from_v"], got["to_v"], got["rows"]),
            ("migrated", 1, 2, 4),
        )
        self.rw()  # already v2: no second line
        self.assertEqual(
            len((self.db.parent / "poller.log").read_text().splitlines()), 1
        )

    def _poller_events(self) -> list[dict[str, Any]]:
        """Return the parsed poller.log lines."""
        text = (self.db.parent / "poller.log").read_text()
        return [json.loads(line) for line in text.splitlines()]

    def test_losing_the_race_logs_migrate_raced(self) -> None:
        make_v1(self.db)
        conn = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA user_version=2")  # the winner already committed
        store._migrate_logged(conn, self.db.parent)
        (got,) = self._poller_events()
        self.assertEqual(got["event"], "migrate_raced")
        self.assertNotIn("rows", got)

    def test_failed_migration_logs_the_exception_class(self) -> None:
        make_v1(self.db)
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.close()  # any use now raises ProgrammingError
        with self.assertRaises(sqlite3.ProgrammingError):
            store._migrate_logged(conn, self.db.parent)
        (got,) = self._poller_events()
        self.assertEqual(
            (got["event"], got["exc"]), ("migrate_failed", "ProgrammingError")
        )

    def test_read_only_connect_refuses_v1_so_hooks_fail_open(self) -> None:
        make_v1(self.db)
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_ro(self.db)


class SwapFailureTests(StoreCase):
    """A swap that fails for real rolls back, reports the true error."""

    def v1_with_clash(self) -> None:
        """Make a v1 file whose swap fails when it creates knowledge_v2."""
        make_v1(self.db)
        raw = sqlite3.connect(self.db, isolation_level=None)
        raw.execute("CREATE TABLE knowledge_v2 (x INTEGER)")
        raw.close()

    def assert_still_v1(self) -> None:
        """Check the file is the untouched v1 ledger."""
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(rows(raw, "SELECT count(*) FROM knowledge"), [(4,)])
        cols = [r[1] for r in raw.execute("PRAGMA table_info(knowledge)")]
        self.assertNotIn("tags", cols)

    def test_foreign_keys_restored_after_a_real_mid_swap_failure(self) -> None:
        for before in (1, 0):
            with self.subTest(foreign_keys_before=before):
                self.v1_with_clash()
                conn = sqlite3.connect(self.db, isolation_level=None)
                self.addCleanup(conn.close)
                conn.execute(f"PRAGMA foreign_keys={before}")
                with self.assertRaises(sqlite3.OperationalError):
                    migrate_v1_to_v2(conn)
                self.assertEqual(
                    conn.execute("PRAGMA foreign_keys").fetchone()[0], before
                )
                self.assertFalse(conn.in_transaction)
                self.assert_still_v1()
                conn.close()
                self.db.unlink()

    def test_connect_rw_logs_the_true_exception_class(self) -> None:
        self.v1_with_clash()
        with self.assertRaises(sqlite3.OperationalError):
            store.connect_rw(self.db, fullfsync=False)
        text = (self.db.parent / "poller.log").read_text()
        (got,) = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            (got["event"], got["exc"]), ("migrate_failed", "OperationalError")
        )

    def test_failing_commit_rollback_and_pragma_keep_the_first_error(
        self,
    ) -> None:
        make_v1(self.db)

        class Broken(sqlite3.Connection):
            """COMMIT fails, then so do ROLLBACK and the pragma restore."""

            def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("commit failed")
                if sql == "ROLLBACK":
                    raise sqlite3.DatabaseError("rollback failed")
                if sql == "PRAGMA foreign_keys=ON":
                    raise sqlite3.DatabaseError("pragma failed")
                return super().execute(sql, *args)

        conn = sqlite3.connect(self.db, isolation_level=None, factory=Broken)
        # The prior state, set past the override that breaks the restore.
        sqlite3.Connection.execute(conn, "PRAGMA foreign_keys=ON")
        with self.assertRaisesRegex(sqlite3.OperationalError, "commit failed"):
            migrate_v1_to_v2(conn)
        conn.close()
        self.assert_still_v1()

    def swap_with(self, tamper: str) -> sqlite3.Connection:
        """Run the migration with ``tamper`` run right after the copy."""
        make_v1(self.db)

        class Tampering(sqlite3.Connection):
            """Damages the new table once the copy statement has run."""

            def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
                done = super().execute(sql, *args)
                if sql.startswith("INSERT INTO knowledge_v2"):
                    super().execute(tamper)
                return done

        conn = sqlite3.connect(
            self.db, isolation_level=None, factory=Tampering
        )
        self.addCleanup(conn.close)
        return conn

    def test_lost_rows_roll_the_migration_back(self) -> None:
        conn = self.swap_with("DELETE FROM knowledge_v2 WHERE id = 1")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "row count"):
            migrate_v1_to_v2(conn)
        self.assert_still_v1()

    def test_a_new_foreign_key_violation_rolls_the_migration_back(
        self,
    ) -> None:
        conn = self.swap_with("UPDATE knowledge_v2 SET supersedes = 99")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "foreign key"):
            migrate_v1_to_v2(conn)
        self.assert_still_v1()

    def test_an_old_foreign_key_violation_does_not_block_the_upgrade(
        self,
    ) -> None:
        make_v1(self.db)
        raw = sqlite3.connect(self.db, isolation_level=None)
        raw.execute(
            "INSERT INTO citation(knowledge_id, provider, thread_id, line,"
            " part, line_sha256, role, kind, quote) VALUES (99, 'claude',"
            " 't', 9, 1, 'h', 'user', 'prompt', 'orphan')"
        )
        raw.close()
        conn = self.rw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_two_real_connections_racing_migrate_once(self) -> None:
        make_v1(self.db)
        gate = threading.Barrier(2, timeout=10)
        real = store.migrate_v1_to_v2

        def after_both_saw_v1(conn: sqlite3.Connection) -> int | None:
            gate.wait()  # neither starts before both read user_version 1
            return real(conn)

        failures: list[BaseException] = []

        def open_store() -> None:
            try:
                store.connect_rw(self.db, fullfsync=False).close()
            except BaseException as exc:  # reported on the main thread
                failures.append(exc)

        with mock.patch.object(store, "migrate_v1_to_v2", after_both_saw_v1):
            workers = [threading.Thread(target=open_store) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
        self.assertEqual(failures, [])
        text = (self.db.parent / "poller.log").read_text()
        events = sorted(json.loads(x)["event"] for x in text.splitlines())
        self.assertEqual(events, ["migrate_raced", "migrated"])
        self.assertEqual(
            self.rw().execute("PRAGMA user_version").fetchone()[0], 2
        )


if __name__ == "__main__":
    unittest.main()
