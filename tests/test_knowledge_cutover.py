"""Exercise exact governed copies without publishing or opening live stores."""

from __future__ import annotations

import json
import sqlite3
from unittest import mock

from install.cutover import copy_governed_state
from muninn import store
from tests.knowledge_cutover_support import (
    cursor_history,
    digest,
    legacy_schema,
)
from tests.test_review_commit import CommitCase

_BASE = (
    "scope",
    "scope_path",
    "source",
    "event",
    "usage",
    "source_issue",
    "tombstone",
)
_GOVERNED = (
    "store_meta",
    "review_control",
    "review_source",
    "review_request",
    "review_attempt",
    "review_call",
    "review_evidence",
    "review_input",
    "knowledge",
    "knowledge_version",
    "knowledge_claim",
    "knowledge_support",
    "unresolved",
    "review_receipt",
)


class KnowledgeCutoverTests(CommitCase):
    """Reject unknown formats and preserve durable content in atomic copies."""

    def setUp(self) -> None:
        super().setUp()
        self.target = sqlite3.connect(":memory:", isolation_level=None)
        self.target.row_factory = sqlite3.Row
        self.target.execute("PRAGMA foreign_keys=ON")
        store.initialize_store(self.target)
        self.addCleanup(self.target.close)

    def legacy(self) -> sqlite3.Connection:
        """Build the one permitted historical shape with DB-only history."""
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.executescript(legacy_schema())
        for table in _BASE:
            rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
            if rows:
                marks = ",".join("?" for _ in rows[0])
                conn.executemany(f"INSERT INTO {table} VALUES({marks})", rows)
        return conn

    def test_historical_schema_one_is_not_new_format(self) -> None:
        source = self.legacy()
        source.execute("PRAGMA user_version=1")
        before = digest(self.target, _BASE + _GOVERNED)
        with self.assertRaisesRegex(ValueError, "cutover_source_mismatch"):
            copy_governed_state(source, self.target)
        self.assertEqual(digest(self.target, _BASE + _GOVERNED), before)

    def test_current_copy_preserves_scope_tombstones_and_db_only_history(
        self,
    ) -> None:
        self.save(self.result([self.note()]))
        self.conn.execute("UPDATE source SET status='missing'")
        self.conn.execute(
            "INSERT INTO source_issue VALUES(1,99,1.0,'invalid_json')"
        )
        self.conn.execute(
            "INSERT INTO tombstone VALUES"
            "(7,1.0,'cursor','thread',NULL,'gone',NULL,NULL)"
        )
        self.path.unlink()
        before = digest(self.conn, _BASE + _GOVERNED)
        copied = copy_governed_state(self.conn, self.target)
        self.assertEqual(digest(self.target, _BASE + _GOVERNED), before)
        self.assertEqual(copied["source_issue"], 1)
        self.assertEqual(copied["tombstone"], 1)
        self.assertEqual(copied["review_receipt"], 1)
        self.assertEqual(
            self.target.execute("PRAGMA foreign_key_check").fetchall(), []
        )
        self.assertEqual(
            self.target.execute("PRAGMA quick_check").fetchone()[0], "ok"
        )
        self.assertEqual(
            self.target.execute(
                "SELECT count(*) FROM event_fts WHERE event_fts MATCH 'Python'"
            ).fetchone()[0],
            1,
        )

    def test_legacy_knowledge_snapshot_only(self) -> None:
        cursor_history(self.conn)
        source = self.legacy()
        source.execute(
            "INSERT INTO knowledge(id,scope_id,kind,text,status,actor,"
            "created_at)"
            " VALUES(1,1,'fact','old ledger','current','user',1.0)"
        )
        before = digest(source, (*_BASE, "knowledge"))
        copied = copy_governed_state(source, self.target)
        self.assertEqual(digest(self.target, _BASE), digest(source, _BASE))
        self.assertEqual(digest(source, (*_BASE, "knowledge")), before)
        self.assertNotIn("knowledge", copied)
        self.assertEqual(
            self.target.execute("SELECT count(*) FROM knowledge").fetchone()[
                0
            ],
            0,
        )
        self.assertEqual(
            self.target.execute(
                "SELECT admission,policy_json FROM review_control"
            ).fetchone()[:],
            (0, None),
        )

    def test_every_durable_family_preserves_history_and_cursor_parentage(
        self,
    ) -> None:
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
                        "question": "What comes next?",
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
        self.path.unlink()
        before = digest(self.conn, _BASE + _GOVERNED)
        counts = copy_governed_state(self.conn, self.target)
        self.assertTrue(all(counts[table] > 0 for table in _BASE + _GOVERNED))
        self.assertEqual(digest(self.target, _BASE + _GOVERNED), before)
        self.assertEqual(digest(self.conn, _BASE + _GOVERNED), before)
        self.assertEqual(
            self.target.execute(
                "SELECT parent_event_id FROM event WHERE id=901"
            ).fetchone()[0],
            900,
        )
        self.assertEqual(
            self.target.execute(
                "SELECT current_revision FROM knowledge WHERE id=?",
                (saved.knowledge_id,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.target.execute(
                "SELECT dependency_keys_json FROM knowledge_claim "
                "WHERE knowledge_id=? AND revision=1 "
                "AND claim_key='step/next'",
                (saved.knowledge_id,),
            ).fetchone()[0],
            '["step/first"]',
        )

    def test_unknown_shape_refused_before_target_write(self) -> None:
        for sql in ("CREATE TABLE surprise(value TEXT)",):
            with self.subTest(sql=sql):
                source = self.legacy()
                source.execute(sql)
                with self.assertRaisesRegex(
                    ValueError, "cutover_source_mismatch"
                ):
                    copy_governed_state(source, self.target)
                self.assertEqual(
                    self.target.execute(
                        "SELECT count(*) FROM event"
                    ).fetchone()[0],
                    0,
                )

    def test_copy_interruption_never_activates_partial_store(self) -> None:
        self.save(self.result([self.note()]))
        before = digest(self.target, _BASE + _GOVERNED)
        self.target.execute(
            "CREATE TEMP TRIGGER fail BEFORE INSERT ON knowledge_support "
            "BEGIN SELECT RAISE(ABORT,'interrupted'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "interrupted"):
            copy_governed_state(self.conn, self.target)
        self.assertEqual(digest(self.target, _BASE + _GOVERNED), before)
        self.assertFalse(self.target.in_transaction)
        self.assertFalse(self.conn.in_transaction)

    def test_transaction_and_nonempty_target_refused(self) -> None:
        for conn in (self.conn, self.target):
            conn.execute("BEGIN")
            with self.assertRaisesRegex(ValueError, "transaction"):
                copy_governed_state(self.conn, self.target)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
        self.target.execute(
            "INSERT INTO scope VALUES(90,'untouched','untouched','dir')"
        )
        with self.assertRaisesRegex(ValueError, "cutover_target_not_empty"):
            copy_governed_state(self.conn, self.target)
        self.assertEqual(
            self.target.execute("SELECT key FROM scope").fetchone()[0],
            "untouched",
        )

    def test_foreign_key_violation_refuses_atomic_copy(self) -> None:
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute(
            "INSERT INTO review_input VALUES('missing','missing')"
        )
        before = digest(self.target, _BASE + _GOVERNED)
        with self.assertRaisesRegex(ValueError, "cutover_foreign_keys"):
            copy_governed_state(self.conn, self.target)
        self.assertEqual(digest(self.target, _BASE + _GOVERNED), before)

    def test_copy_uses_no_process_lock_or_root_connection(self) -> None:
        with (
            mock.patch(
                "subprocess.Popen", side_effect=AssertionError("process")
            ),
            mock.patch(
                "muninn.store.writer_lock", side_effect=AssertionError("lock")
            ),
            mock.patch(
                "sqlite3.connect", side_effect=AssertionError("connection")
            ),
        ):
            copy_governed_state(self.conn, self.target)
        self.assertEqual(
            digest(self.target, _BASE + _GOVERNED),
            digest(self.conn, _BASE + _GOVERNED),
        )

    def test_fts_is_derived_from_eligible_claims_without_stale_rows(
        self,
    ) -> None:
        saved = self.save(self.result([self.note()])).applied[0]
        self.conn.execute("DELETE FROM knowledge_fts")
        self.conn.execute(
            "INSERT INTO knowledge_fts VALUES('stale',99,'phantom')"
        )
        copy_governed_state(self.conn, self.target)
        self.assertEqual(
            [
                tuple(row)
                for row in self.target.execute("SELECT * FROM knowledge_fts")
            ],
            [(saved.knowledge_id, 1, "Use Python.")],
        )
        self.assertEqual(
            self.target.execute(
                "SELECT count(*) FROM knowledge_fts "
                "WHERE knowledge_fts MATCH 'phantom'"
            ).fetchone()[0],
            0,
        )

    def test_expired_content_preserved_but_excluded_from_fts(self) -> None:
        self.save(self.result([self.note()]))
        expiry = json.dumps(
            {
                "at": 1.0,
                "condition": None,
                "evidence_refs": [self.evidence[0].id],
            }
        )
        self.conn.execute(
            "UPDATE knowledge_version SET expiry_json=?", (expiry,)
        )
        before = digest(self.conn, _GOVERNED)
        copy_governed_state(self.conn, self.target)
        self.assertEqual(digest(self.target, _GOVERNED), before)
        self.assertEqual(
            self.target.execute(
                "SELECT count(*) FROM knowledge_fts"
            ).fetchone()[0],
            0,
        )

    def test_overlapping_task_scopes_do_not_duplicate_fts(self) -> None:
        first = self.save(self.result([self.note()])).applied[0]
        self.scope = self.scope.model_copy(update={"task": "build"})
        self.context = self.start(2, 2)
        second = self.save(self.result([self.note()])).applied[0]
        copy_governed_state(self.conn, self.target)
        ids = [
            row[0]
            for row in self.target.execute(
                "SELECT knowledge_id FROM knowledge_fts"
            )
        ]
        self.assertCountEqual(ids, [first.knowledge_id, second.knowledge_id])

    def test_unknown_charges_and_erased_content_are_preserved(self) -> None:
        self.save(self.result([self.note()]))
        self.conn.execute(
            "INSERT INTO review_call VALUES(?,1,'account','digest','unknown',"
            "11.0,12.0,NULL,'{}','{}',NULL,'4.20','USD',NULL,NULL)",
            (self.context.attempt_id,),
        )
        self.conn.execute("UPDATE knowledge SET state='erased'")
        self.conn.execute(
            "UPDATE knowledge_version SET content_json=NULL,erased_at=15.0"
        )
        self.conn.execute(
            "UPDATE knowledge_claim SET text=NULL,state='erased'"
        )
        before = digest(self.conn, _GOVERNED)
        copy_governed_state(self.conn, self.target)
        self.assertEqual(digest(self.target, _GOVERNED), before)
        self.assertEqual(
            self.target.execute(
                "SELECT count(*) FROM knowledge_fts"
            ).fetchone()[0],
            0,
        )

    def test_source_rollback_error_preserves_original_copy_failure(
        self,
    ) -> None:
        self.save(self.result([self.note()]))
        before = digest(self.target, _BASE + _GOVERNED)
        self.target.execute(
            "CREATE TEMP TRIGGER fail BEFORE INSERT ON knowledge_support "
            "BEGIN SELECT RAISE(ABORT,'original copy failure'); END"
        )

        def authorize(
            action: int,
            first: str | None,
            second: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_TRANSACTION and first == "ROLLBACK":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.conn.set_authorizer(authorize)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "original copy failure"
            ):
                copy_governed_state(self.conn, self.target)
        finally:
            self.conn.set_authorizer(None)
            self.conn.rollback()
        self.assertFalse(self.target.in_transaction)
        self.assertEqual(digest(self.target, _BASE + _GOVERNED), before)
