"""muninn rebuild keeps knowledge, tombstones and missing events."""

from __future__ import annotations

from typing import Any

from muninn import store
from tests.cli_support import CANARY, CliCase
from tests.test_ingest import TID


class RebuildTests(CliCase):
    """Rebuild a store holding knowledge, a tombstone and a missing file."""

    def setUp(self) -> None:
        super().setUp()
        self.keep = self.session(TID, "keep this prompt", "kept")
        self.gone = self.session("thr-gone", "file deleted later", "gone")
        self.session("thr-erased", f"{CANARY} erased", "erased")
        self.run_ingest()
        sid = self.conn.execute("SELECT id FROM scope LIMIT 1").fetchone()[0]
        self.kid = self.conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'decision', 'use rebuild', 'current',"
            " 'user', 1)",
            (sid,),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO citation(knowledge_id, provider, thread_id, line,"
            " part, line_sha256, role, kind, quote, span_start, span_end)"
            " VALUES (?, 'codex', ?, 2, 1, 'h', 'user', 'prompt',"
            " 'keep this', 0, 9)",
            (self.kid, TID),
        )
        self.conn.execute(
            "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
            " VALUES (?, 'add', 'user', 1)",
            (self.kid,),
        )
        self.assertEqual(
            self.muninn("erase", "--session", "thr-erased", "--yes")[0], 0
        )
        self.gone.unlink()
        self.run_ingest()  # thr-gone is now missing, its events kept
        self.conn.close()

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        """Run a read-only query against the rebuilt database.

        Args:
            sql: Statement to execute.

        Returns:
            Every result row as a tuple.
        """
        conn = store.connect_ro(store.db_path(self.home))
        try:
            return [tuple(r) for r in conn.execute(sql)]
        finally:
            conn.close()

    def test_rebuild_preserves_knowledge_tombstones_missing_events(
        self,
    ) -> None:
        before = store.db_path(self.home).stat().st_ino
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, out)
        self.assertIs(out["old_readable"], True)
        # A new inode proves the database was rebuilt, not repaired in place.
        after = store.db_path(self.home).stat().st_ino
        self.assertNotEqual(after, before)
        self.assertEqual(
            self.rows("SELECT id, text, status FROM knowledge"),
            [(self.kid, "use rebuild", "current")],
        )
        self.assertEqual(self.rows("SELECT count(*) FROM citation"), [(1,)])
        self.assertEqual(
            self.rows("SELECT count(*) FROM knowledge_log"), [(1,)]
        )
        threads = dict(self.rows("SELECT thread_id, status FROM source"))
        self.assertEqual(threads, {TID: "active", "thr-gone": "missing"})
        texts = {r[0] for r in self.rows("SELECT text FROM event")}
        self.assertLessEqual({"keep this prompt", "file deleted later"}, texts)
        self.assertFalse(any(CANARY in t for t in texts))  # tombstone held
        self.assertEqual(
            self.rows("SELECT level FROM tombstone"), [("session",)]
        )
        leftovers = [
            p.name
            for p in self.home.iterdir()
            if "rebuild" in p.name or p.name.endswith("-journal")
        ]
        self.assertEqual(leftovers, [])
        mode = store.db_path(self.home).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        found = self.rows(
            "SELECT count(*) FROM knowledge_fts"
            " WHERE knowledge_fts MATCH 'rebuild'"
        )
        self.assertEqual(found, [(1,)])

    def test_rebuild_from_an_unreadable_store_keeps_tombstones(self) -> None:
        db = store.db_path(self.home)
        db.write_bytes(b"not a database" * 100)
        code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["old_readable"]), (0, False))
        self.assertGreaterEqual(out["reapplied_tombstones"], 1)
        texts = {r[0] for r in self.rows("SELECT text FROM event")}
        self.assertIn("keep this prompt", texts)
        self.assertFalse(any(CANARY in t for t in texts))
