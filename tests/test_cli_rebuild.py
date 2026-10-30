"""Rebuild only verified current stores, retaining governed history."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from muninn import cli_rebuild, store, store_copy
from tests.cli_support import CANARY, CliCase
from tests.knowledge_cutover_support import (
    cursor_history,
    digest,
    legacy_schema,
)
from tests.knowledge_retirement_support import native_note
from tests.review_pause_support import initialize
from tests.store_support import assert_private
from tests.test_ingest import TID
from tests.test_review_commit import CommitCase


class RebuildTests(CliCase):
    """Rebuild actual normalized rows and physical provider fixtures."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.close()
        initialize(self.home)
        self.keep = self.session(TID, "Please keep this prompt", "kept")
        self.assertEqual(self.muninn("ingest")[0], 0)
        native_note(self.home, self.roots, "use rebuild")
        self.gone = self.session("thr-gone", "file deleted later", "gone")
        self.session("thr-erased", f"{CANARY} erased", "erased")
        self.assertEqual(self.muninn("ingest")[0], 0)
        with closing(store.connect_rw(store.db_path(self.home))) as conn:
            self.kid = conn.execute("SELECT id FROM knowledge").fetchone()[0]
            cursor_history(conn)
        self.assertEqual(
            self.muninn("erase", "--session", "thr-erased", "--yes")[0], 0
        )
        self.gone.unlink()
        self.assertEqual(self.muninn("ingest")[0], 0)

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        """Read the actual live file after maintenance has released it."""
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            return [tuple(row) for row in conn.execute(sql)]

    def test_rebuild_preserves_knowledge_tombstones_missing_events(
        self,
    ) -> None:
        before = store.db_path(self.home).stat().st_ino
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, out)
        self.assertIs(out["old_readable"], True)
        self.assertNotEqual(store.db_path(self.home).stat().st_ino, before)
        self.assertEqual(
            self.rows("SELECT id,state FROM knowledge"), [(self.kid, "active")]
        )
        body = json.loads(
            self.rows("SELECT content_json FROM knowledge_version")[0][0]
        )
        self.assertEqual(body["content"], "use rebuild")
        self.assertEqual(
            self.rows("SELECT count(*) FROM knowledge_support"), [(1,)]
        )
        self.assertEqual(
            self.rows("SELECT count(*) FROM review_receipt"), [(1,)]
        )
        self.assertEqual(
            dict(self.rows("SELECT thread_id,status FROM source")),
            {
                TID: "active",
                "thr-gone": "missing",
                "cursor-missing": "missing",
            },
        )
        texts = {row[0] for row in self.rows("SELECT text FROM event")}
        self.assertLessEqual(
            {
                "Please keep this prompt",
                "file deleted later",
                "Cursor row 900",
                "Cursor row 901",
            },
            texts,
        )
        self.assertFalse(any(CANARY in text for text in texts))
        self.assertEqual(
            self.rows("SELECT parent_event_id FROM event WHERE id=901"),
            [(900,)],
        )
        self.assertEqual(
            self.rows("SELECT calls,errors FROM usage WHERE source_id=900"),
            [(3, 1)],
        )
        self.assertEqual(
            self.rows("SELECT code FROM source_issue WHERE source_id=900"),
            [("invalid_json",)],
        )
        self.assertEqual(
            self.rows("SELECT level FROM tombstone"), [("session",)]
        )
        archive = self.home / out["old_kept_as"]
        self.assertTrue(archive.name.startswith(store.RECOVERY_PREFIX))
        assert_private(self, archive)
        assert_private(self, store.db_path(self.home))
        self.assertFalse(out["recovery_required"])
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.rows("PRAGMA quick_check"), [("ok",)])
        self.assertEqual(
            self.rows(
                "SELECT count(*) FROM knowledge_fts "
                "WHERE knowledge_fts MATCH 'rebuild'"
            ),
            [(1,)],
        )
        self.assertFalse((self.home / "muninn.sqlite.rebuild").exists())

    def test_unreadable_store_is_refused_without_replacement(self) -> None:
        db = store.db_path(self.home)
        junk = b"not a database" * 100
        db.write_bytes(junk)
        code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "store_unavailable"))
        self.assertEqual(db.read_bytes(), junk)
        self.assertFalse(out["publication_possible"])
        self.assertFalse(
            any(store.RECOVERY_PREFIX in p.name for p in self.home.iterdir())
        )

    def test_hot_journal_refuses_without_touching_source(self) -> None:
        db = store.db_path(self.home)
        before = db.read_bytes()
        sidecar = self.home / "muninn.sqlite-journal"
        sidecar.write_bytes(b"\0" * 512)
        sidecar.chmod(0o600)
        code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["reason"]), (2, "source_changed"))
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(sidecar.read_bytes(), b"\0" * 512)
        self.assertFalse(
            any(store.RECOVERY_PREFIX in p.name for p in self.home.iterdir())
        )


class RebuildFormatTests(CliCase):
    """Historical numeric schemas never stand in for the current marker."""

    def test_unmarked_historical_schemas_preserve_all_bytes(self) -> None:
        self.conn.close()
        db = store.db_path(self.home)
        for version in (1, 2, 3):
            with self.subTest(version=version):
                db.unlink()
                with closing(sqlite3.connect(db)) as conn:
                    conn.executescript(legacy_schema())
                    conn.execute(f"PRAGMA user_version={version}")
                    conn.execute(
                        "INSERT INTO scope "
                        "VALUES(1,'loop:run-3','loop:run-3','dir')"
                    )
                    conn.execute(
                        "INSERT INTO knowledge(id,scope_id,kind,text,status,"
                        "actor,"
                        "created_at,confidence,valid_until,sensitivity,tags) "
                        "VALUES(1,1,'lesson','typed','current','user',1,"
                        "'observed',99,'restricted','a,b')"
                    )
                    conn.execute(
                        "INSERT INTO knowledge(id,scope_id,kind,text,status,"
                        "actor,"
                        "created_at,contradicts) "
                        "VALUES(2,1,'fact','second','current','user',1,1)"
                    )
                    conn.execute(
                        "UPDATE knowledge SET status='superseded' WHERE id=1"
                    )
                    conn.execute(
                        "UPDATE knowledge SET supersedes=1 WHERE id=2"
                    )
                    for ident, status in ((3, "retracted"), (4, "erased")):
                        conn.execute(
                            "INSERT INTO knowledge(id,scope_id,kind,text,"
                            "status,actor,created_at) "
                            "VALUES(?,1,'fact','old',?,'user',1)",
                            (ident, status),
                        )
                    conn.commit()
                db.chmod(0o600)
                before = db.read_bytes()
                code, out, _ = self.muninn("rebuild")
                self.assertEqual(
                    (code, out["error"]), (4, "store_unavailable")
                )
                self.assertEqual(db.read_bytes(), before)
                self.assertFalse(out["publication_possible"])
                self.assertFalse(
                    (self.home / "muninn.sqlite.rebuild").exists()
                )


class GovernedRebuildTests(CommitCase):
    """Preserve every durable family through actual standalone publication."""

    def history(self) -> None:
        """Create durable history across all governed families."""
        claim = {"text": "Use Python.", "evidence_refs": [self.evidence[0].id]}
        guide = {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": "Python guide",
            "summary": claim,
            "applicability": [],
            "decision_points": [],
            "pitfalls": [],
            "reason": "Synthetic procedure.",
            "steps": [
                {"key": "first", "claim": claim, "depends_on": []},
                {"key": "next", "claim": claim, "depends_on": ["first"]},
            ],
        }
        saved = self.save(self.result([guide])).applied[0]
        self.context = self.start(2, 2)
        self.save(
            self.result(
                [
                    self.note(
                        action="replace",
                        knowledge_id=saved.knowledge_id,
                        expected_revision=1,
                    )
                ]
            )
        )
        self.context = self.start(3, 3)
        self.save(
            self.result(
                [],
                [
                    {
                        "claim": "Unresolved fixture",
                        "question": "What next?",
                        "affected": [],
                        "evidence_refs": [self.evidence[1].id],
                    }
                ],
            )
        )
        self.conn.execute(
            "INSERT INTO review_call VALUES(?,1,'account','digest','unknown',"
            "11.0,12.0,NULL,'{}','{}',NULL,'4.20','USD',NULL,NULL)",
            (self.context.attempt_id,),
        )
        cursor_history(self.conn)
        self.conn.execute(
            "INSERT INTO tombstone VALUES"
            "(7,1.0,'cursor','thread',NULL,'gone',NULL,NULL)"
        )
        self.conn.execute("UPDATE source SET status='missing'")
        self.path.unlink()

    def test_histories_unknown_charges_and_db_only_data_survive(self) -> None:
        self.history()
        tables = store_copy.BASE_TABLES + store_copy.GOVERNED_TABLES
        immutable = tuple(
            table for table in tables if table != "review_control"
        )
        before = digest(self.conn, immutable)
        control = tuple(self.row("SELECT * FROM review_control"))
        self.conn.close()
        initialize(self.home)
        env = {
            "MUNINN_ROOTS": json.dumps(
                {k: str(v) for k, v in self.roots.items()}
            )
        }
        out = cli_rebuild._run(env, self.home, cli_rebuild._Status())
        empty_tables = (
            "memory_candidate",
            "memory_decision",
            "memory_candidate_support",
        )
        populated_tables = tuple(t for t in tables if t not in empty_tables)
        self.assertTrue(
            all(out["copied"][table] > 0 for table in populated_tables)
        )
        self.assertEqual(
            {table: out["copied"][table] for table in empty_tables},
            {
                "memory_candidate": 0,
                "memory_decision": 0,
                "memory_candidate_support": 0,
            },
        )
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            self.assertEqual(digest(conn, immutable), before)
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM review_control").fetchone()),
                (1, control[1] + 1, *control[2:]),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT current_revision FROM knowledge"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT dependency_keys_json FROM knowledge_claim "
                    "WHERE revision=1 AND claim_key='step/next'"
                ).fetchone()[0],
                '["step/first"]',
            )
            self.assertEqual(
                conn.execute(
                    "SELECT state,cost_reservation_decimal FROM review_call"
                ).fetchone()[:],
                ("unknown", "4.20"),
            )
        with closing(
            store.connect_ro(self.home / out["old_kept_as"])
        ) as archive:
            self.assertEqual(digest(archive, immutable), before)
            self.assertEqual(
                archive.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )
