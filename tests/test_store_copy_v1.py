"""Validate frozen marked-v1 sources and explicit current-v2 projections."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from pathlib import Path
from unittest import mock

from muninn import store, store_copy
from tests.store_support import StoreCase
from tests.test_store_copy_validation import read_only

FIXTURES = {
    "base": "535cd228a984e4d5cd6f98787a33c657359bf780753c73ec33885a2ed62ddc3c",
    "state": (
        "9ebab010e421fd9d431a439dc6aa25be7b47eaeada00c160ae05d63a35b6592f"
    ),
}


def populate(conn: sqlite3.Connection) -> None:
    """Insert old-column rows independently of current runtime factories."""
    conn.executescript("""
    UPDATE review_control SET admission=1,policy_json='{}',policy_digest='old';
    INSERT INTO review_request VALUES (
      'R_one','old-key','codex','thread','thread',1,2,'old-hash',
      '{"project_id":"/repo","task":null}','{}','old','explicit','review',
      'completed','A_one',1.5,NULL,NULL);
    INSERT INTO review_attempt VALUES (
      'A_one','R_one',1,NULL,'{}',2.0,2.0,3.0,'completed',NULL,NULL);
    INSERT INTO review_evidence VALUES (
      'E_one','codex','thread','thread',1,1,'old-hash','source','projection',
      '{}','{}','selector','owner_statement','direct_owner',
      '{"project_id":"/repo","task":null}','Old evidence','live',1.0);
    INSERT INTO review_input VALUES ('R_one','E_one');
    INSERT INTO knowledge VALUES ('K_one',1,'withheld',NULL,NULL);
    INSERT INTO knowledge_version VALUES (
      'K_one',1,'R_one','note','fact','{"project_id":"/repo","task":null}',
      '{}','Old reason','review',NULL,3.0,NULL);
    INSERT INTO knowledge_claim VALUES (
      'K_one',1,'note','Old claim','[]','withheld');
    INSERT INTO knowledge_support VALUES ('K_one',1,'note','E_one');
    INSERT INTO unresolved VALUES (
      'U_one','R_one','{}','[]','["E_one"]',3.0,'open');
    INSERT INTO review_receipt VALUES (
      'R_one','A_one','[]','["U_one"]',3.0,'completed_unresolved',NULL);
    """)


class FrozenV1Tests(StoreCase):
    """Keep old DDL as an independent oracle for runtime refusal and copy."""

    def old(self, shape: str) -> sqlite3.Connection:
        """Open a populated frozen old schema with no current factory calls."""
        path = self.home / f"old-{shape}.sqlite"
        store.ensure_private_dir(self.home)
        conn = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(conn.close)
        sql = (
            Path(__file__).parent
            / "fixtures"
            / "installer"
            / f"marked-v1-{shape}.sql"
        )
        conn.executescript(sql.read_text())
        path.chmod(0o600)
        populate(conn)
        return conn

    def test_frozen_shapes_are_read_only_supported_sources_but_runtime_refuses(
        self,
    ) -> None:
        for shape, fingerprint in FIXTURES.items():
            with self.subTest(shape=shape):
                conn = self.old(shape)
                path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
                rows = conn.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                    "WHERE sql IS NOT NULL ORDER BY type,name"
                ).fetchall()
                self.assertEqual(
                    hashlib.sha256(
                        json.dumps(rows, separators=(",", ":")).encode()
                    ).hexdigest(),
                    fingerprint,
                )
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0], 1
                )
                self.assertEqual(
                    conn.execute("SELECT * FROM store_meta").fetchall(),
                    [(1, "muninn-knowledge", 1)],
                )
                before = path.read_bytes()
                projection = tuple(conn.iterdump())
                inode, changes = path.stat().st_ino, conn.total_changes
                for active in (False, True):
                    if active:
                        conn.execute("BEGIN")
                    conn.set_authorizer(read_only)
                    try:
                        with (
                            mock.patch.object(
                                store,
                                "writer_lock",
                                side_effect=AssertionError,
                            ),
                            mock.patch(
                                "sqlite3.connect", side_effect=AssertionError
                            ),
                        ):
                            self.assertEqual(
                                store_copy.validate_source(conn), 1
                            )
                    finally:
                        conn.set_authorizer(None)
                    self.assertEqual(conn.in_transaction, active)
                    self.assertEqual(conn.total_changes, changes)
                conn.rollback()
                for opener in (store.connect_ro, store.connect_rw):
                    with (
                        self.assertRaisesRegex(
                            store.StoreUnavailableError, "^cutover_required$"
                        ),
                        contextlib.closing(opener(path)),
                    ):
                        pass
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(path.stat().st_ino, inode)
                    self.assertEqual(tuple(conn.iterdump()), projection)

    def test_copy_maps_only_marker_and_nullable_application_columns(
        self,
    ) -> None:
        for shape in FIXTURES:
            with self.subTest(shape=shape):
                source = self.old(shape)
                target = store.connect_rw(self.home / f"target-{shape}.sqlite")
                self.addCleanup(target.close)
                original = tuple(source.iterdump())
                counts = store_copy.copy_governed_state(source, target)
                self.assertEqual(
                    target.execute("SELECT * FROM store_meta").fetchall()[0][
                        "version"
                    ],
                    2,
                )
                self.assertEqual(
                    target.execute("PRAGMA user_version").fetchone()[0], 2
                )
                for table in counts:
                    if table == "store_meta":
                        continue
                    columns = ",".join(
                        '"' + r[1] + '"'
                        for r in source.execute(f"PRAGMA table_info({table})")
                    )
                    self.assertEqual(
                        [
                            tuple(r)
                            for r in source.execute(
                                f"SELECT {columns} FROM {table}"
                            )
                        ],
                        [
                            tuple(r)
                            for r in target.execute(
                                f"SELECT {columns} FROM {table}"
                            )
                        ],
                    )
                self.assertEqual(
                    [
                        tuple(r)
                        for r in target.execute(
                            "SELECT candidate_id,decision_revision "
                            "FROM review_request"
                        )
                    ],
                    [(None, None)],
                )
                for table in (
                    "memory_candidate",
                    "memory_decision",
                    "memory_candidate_support",
                ):
                    self.assertEqual(
                        target.execute(
                            f"SELECT count(*) FROM {table}"
                        ).fetchone()[0],
                        0,
                    )
                self.assertEqual(tuple(source.iterdump()), original)
                self.assertFalse(source.in_transaction)
                self.assertFalse(target.in_transaction)
                self.assertEqual(store_copy.validate_source(target), 2)

    def test_projection_failure_restores_target_and_snapshot_ownership(
        self,
    ) -> None:
        source = self.old("base")
        target = store.connect_rw(self.home / "target.sqlite")
        self.addCleanup(target.close)
        target.execute(
            "CREATE TEMP TRIGGER fail BEFORE INSERT ON review_input "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        before = tuple(target.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            store_copy.copy_governed_state(source, target)
        self.assertEqual(tuple(target.iterdump()), before)
        self.assertFalse(source.in_transaction)
        self.assertFalse(target.in_transaction)
