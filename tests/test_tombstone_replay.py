"""Replay real interrupted erasure through rebuild and public search."""

from __future__ import annotations

from argparse import Namespace
from contextlib import closing

from muninn import cli_rebuild, query, store, tombstones
from tests.knowledge_cutover_support import cursor_history
from tests.tombstone_replay_support import CANARY, CONTROL, TARGET, ReplayCase


class TombstoneReplayTests(ReplayCase):
    """Erasure intent must remove indexed history before replacement."""

    def test_interrupted_session_rebuild_removes_transcript_and_fts(
        self,
    ) -> None:
        target = self.session(TARGET, CANARY, CANARY + " response")
        control = self.session(CONTROL, "control prompt", "control response")
        self.run_ingest()
        providers = {p: p.read_bytes() for p in (target, control)}
        self.assertEqual((self.count("source"), self.count("event")), (2, 4))
        journal = self.interrupt()
        self.assertEqual(
            (self.count("event"), self.count("tombstone")), (4, 0)
        )
        with self.assertRaisesRegex(ValueError, "^erasure_pending$"):
            tombstones.require_current_tombstones(self.conn, self.home)
        self.conn.close()
        code, result = cli_rebuild.rebuild(
            Namespace(), self.env, self.home, {}
        )
        self.assertEqual(code, 0, result)
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM source WHERE thread_id=?", (TARGET,)
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM event_fts WHERE event_fts MATCH ?",
                    (CANARY,),
                ).fetchone()[0],
                0,
            )
            tombstones.require_current_tombstones(conn, self.home)
            self.assertEqual(
                query.search(
                    conn,
                    CANARY,
                    cwd=str(self.repo),
                    env=self.env,
                    all_projects=True,
                )["hits"],
                [],
            )
        self.assertEqual(
            (self.home / "tombstones.jsonl").read_bytes(), journal
        )
        self.assertEqual({p: p.read_bytes() for p in providers}, providers)
        reopened = self.child("reopen", str(self.home))
        self.assertEqual((reopened.returncode, reopened.stderr), (0, ""))

    def test_table_only_intent_scrubs_residual_rows_without_inserting(
        self,
    ) -> None:
        self.session(TARGET, CANARY)
        self.run_ingest()
        self.conn.execute(
            "INSERT INTO tombstone(created_at,provider,level,session_root) "
            "VALUES(1,'codex','session',?)",
            (TARGET,),
        )
        with self.assertRaisesRegex(ValueError, "^erasure_pending$"):
            tombstones.require_current_tombstones(self.conn, self.home)
        self.assertEqual(self.replay(), 0)
        self.assertEqual((self.count("source"), self.count("event")), (0, 0))
        tombstones.require_current_tombstones(self.conn, self.home)
        self.assertFalse((self.home / "tombstones.jsonl").exists())

    def test_already_keyed_journal_does_not_hide_residual_transcript(
        self,
    ) -> None:
        self.session(TARGET, CANARY)
        self.run_ingest()
        self.interrupt()
        self.conn.execute(
            "INSERT INTO tombstone(created_at,provider,level,session_root) "
            "VALUES(1,'codex','session',?)",
            (TARGET,),
        )
        with self.assertRaisesRegex(ValueError, "^erasure_pending$"):
            tombstones.require_current_tombstones(self.conn, self.home)
        self.assertEqual(self.replay(), 0)
        self.assertEqual((self.count("source"), self.count("event")), (0, 0))

    def test_exact_line_hash_keeps_changed_line_and_other_provider(
        self,
    ) -> None:
        self.session(TARGET, CANARY, "surviving reply", CANARY)
        self.session(CONTROL, CANARY)
        self.run_ingest()
        digest = self.conn.execute(
            "SELECT e.line_sha256 FROM event e JOIN source s "
            "ON s.id=e.source_id "
            "WHERE s.thread_id=? AND e.line=2",
            (TARGET,),
        ).fetchone()[0]
        rows = [
            tombstones.tombstone_row(
                "claude", "line", thread_id=CONTROL, line=2, line_sha256=digest
            ),
            tombstones.tombstone_row(
                "codex", "line", thread_id=TARGET, line=3, line_sha256=digest
            ),
            tombstones.tombstone_row(
                "codex", "line", thread_id=TARGET, line=2, line_sha256=digest
            ),
        ]
        tombstones.append_tombstones(self.home, rows)
        self.assertEqual(self.replay(), 3)
        self.assertEqual(
            [r[3] for r in self.events(TARGET)], ["surviving reply", CANARY]
        )
        self.assertEqual([r[3] for r in self.events(CONTROL)], [CANARY])
        self.assertFalse((self.home / "tombstone.key").exists())

    def test_rebuild_retains_db_only_cursor_and_missing_original_controls(
        self,
    ) -> None:
        target = self.session(TARGET, CANARY)
        control = self.session(CONTROL, "missingoriginalcontrol")
        self.run_ingest()
        cursor_history(self.conn)
        self.interrupt()
        target.unlink()
        control.unlink()
        self.conn.close()
        code, result = cli_rebuild.rebuild(
            Namespace(), self.env, self.home, {}
        )
        self.assertEqual(code, 0, result)
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            self.assertEqual(
                [
                    tuple(r)
                    for r in conn.execute(
                        "SELECT provider,thread_id FROM source "
                        "ORDER BY provider,thread_id"
                    )
                ],
                [("codex", CONTROL), ("cursor", "cursor-missing")],
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 3
            )
        for path in (
            store.db_path(self.home),
            *self.home.glob("muninn.sqlite.recovery-*"),
        ):
            self.assertNotIn(CANARY.encode(), path.read_bytes())
            self.assertIn(b"missingoriginalcontrol", path.read_bytes())
