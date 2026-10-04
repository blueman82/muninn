"""muninn rebuild keeps knowledge, tombstones and missing events."""

from __future__ import annotations

from typing import Any

from muninn import store
from tests.cli_support import CANARY, CliCase
from tests.test_ingest import TID
from tests.test_store_migrate import make_v1


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
        self.assertIsNone(out["old_kept_as"])
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
        junk = b"not a database" * 100
        db.write_bytes(junk)
        code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["old_readable"]), (0, False))
        # The unreadable file is set aside whole, private, never overwritten.
        aside = self.home / out["old_kept_as"]
        self.assertTrue(aside.name.startswith(store.UNREADABLE_PREFIX))
        self.assertEqual(aside.read_bytes(), junk)
        self.assertEqual(aside.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.muninn("rebuild")[1]["old_kept_as"])
        self.assertGreaterEqual(out["reapplied_tombstones"], 1)
        texts = {r[0] for r in self.rows("SELECT text FROM event")}
        self.assertIn("keep this prompt", texts)
        self.assertFalse(any(CANARY in t for t in texts))

    def test_hot_journal_blocks_the_move_aside(self) -> None:
        db = store.db_path(self.home)
        db.write_bytes(b"not a database" * 100)
        # Not a real rollback journal, so opening the file cannot settle it.
        (self.home / "muninn.sqlite-journal").write_bytes(b"\x00" * 512)
        code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "hot_journal"))
        self.assertEqual(db.read_bytes(), b"not a database" * 100)
        names = [p.name for p in self.home.iterdir()]
        self.assertFalse(any(store.UNREADABLE_PREFIX in n for n in names))


class RebuildFromV1Tests(CliCase):
    """A rebuild reads an old v1 file and keeps its ledger."""

    def test_ledger_survives_a_rebuild_from_a_v1_file(self) -> None:
        self.conn.close()
        db = store.db_path(self.home)
        db.unlink()
        make_v1(db)
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, out)
        self.assertIs(out["old_readable"], True)
        conn = store.connect_ro(db)
        self.addCleanup(conn.close)
        got = [
            tuple(r)
            for r in conn.execute(
                "SELECT id, status, supersedes, sensitivity FROM knowledge"
            )
        ]
        self.assertEqual(
            got,
            [
                (1, "superseded", None, "normal"),
                (2, "current", 1, "normal"),
                (3, "retracted", None, "normal"),
                (4, "erased", None, "normal"),
            ],
        )

    def test_typed_columns_survive_a_rebuild_from_v2(self) -> None:
        conn = self.conn
        sid = conn.execute(
            "INSERT INTO scope(key, label, kind) VALUES ('/r', 'r', 'git')"
        ).lastrowid
        conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at, confidence, valid_until, sensitivity, tags)"
            " VALUES (?, 'lesson', 'typed', 'current', 'user', 1,"
            " 'observed', 99, 'restricted', 'a,b')",
            (sid,),
        )
        conn.close()
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, out)
        ro = store.connect_ro(store.db_path(self.home))
        self.addCleanup(ro.close)
        got = ro.execute(
            "SELECT kind, confidence, valid_until, sensitivity, tags"
            " FROM knowledge"
        ).fetchone()
        self.assertEqual(
            tuple(got), ("lesson", "observed", 99.0, "restricted", "a,b")
        )
