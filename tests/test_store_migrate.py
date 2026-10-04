"""Schema v1 to v2 migration: ids, chains, FTS, erased rows, crash safety."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from typing import Any

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

    def test_read_only_connect_refuses_v1_so_hooks_fail_open(self) -> None:
        make_v1(self.db)
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_ro(self.db)


if __name__ == "__main__":
    unittest.main()
