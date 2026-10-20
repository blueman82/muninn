"""Read-only validation of the two exact supported source formats."""

from __future__ import annotations

import sqlite3
from unittest import mock

from muninn import store, store_copy
from tests.knowledge_cutover_support import legacy_schema
from tests.store_support import StoreCase


def read_only(
    action: int,
    first: str | None,
    second: str | None,
    database: str | None,
    trigger: str | None,
) -> int:
    """Deny writes and transaction control during actual validation."""
    if action in (
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
    ) or (action == sqlite3.SQLITE_PRAGMA and second is not None):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class SourceValidationTests(StoreCase):
    """Keep real source bytes, identities and caller transactions unchanged."""

    def setUp(self) -> None:
        super().setUp()
        self.conn = self.rw()

    def validate(self, conn: sqlite3.Connection) -> int:
        """Require the public source validator before checking behavior."""
        self.assertTrue(hasattr(store_copy, "validate_source"))
        return store_copy.validate_source(conn)

    def test_current_and_exact_legacy_validate_without_writes_or_roots(
        self,
    ) -> None:
        legacy = self.home / "legacy.sqlite"
        old = sqlite3.connect(legacy, isolation_level=None)
        self.addCleanup(old.close)
        old.executescript(legacy_schema())
        legacy.chmod(0o600)
        old.close()
        for path, version in ((self.db, 1), (legacy, 3)):
            with self.subTest(version=version):
                conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                self.addCleanup(conn.close)
                before, inode = path.read_bytes(), path.stat().st_ino
                schema = conn.execute("SELECT * FROM sqlite_schema").fetchall()
                for transaction in (False, True):
                    if transaction:
                        conn.execute("BEGIN")
                    conn.set_authorizer(read_only)
                    with (
                        mock.patch(
                            "sqlite3.connect", side_effect=AssertionError
                        ),
                        mock.patch.object(
                            store, "writer_lock", side_effect=AssertionError
                        ),
                    ):
                        self.assertEqual(self.validate(conn), version)
                    conn.set_authorizer(None)
                    self.assertEqual(conn.in_transaction, transaction)
                    self.assertEqual(conn.total_changes, 0)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(path.stat().st_ino, inode)
                    self.assertEqual(
                        conn.execute("SELECT * FROM sqlite_schema").fetchall(),
                        schema,
                    )
                conn.execute("ROLLBACK")

    def test_unknown_shape_and_bad_marker_refuse_without_initialization(
        self,
    ) -> None:
        for sql, error in (
            ("CREATE TABLE unknown(value TEXT)", ValueError),
            ("PRAGMA user_version=2", ValueError),
            ("DELETE FROM store_meta", store.StoreUnavailableError),
        ):
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(sql)
            before = tuple(self.conn.iterdump())
            with self.subTest(sql=sql), self.assertRaises(error):
                self.validate(self.conn)
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(tuple(self.conn.iterdump()), before)
            self.conn.execute("ROLLBACK")

    def test_foreign_key_and_real_integrity_violations_are_refused(
        self,
    ) -> None:
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute(
            "INSERT INTO scope_path VALUES('/missing',999,'git')"
        )
        with self.assertRaisesRegex(ValueError, "cutover_foreign_keys"):
            self.validate(self.conn)
        self.conn.execute("DELETE FROM scope_path")
        self.conn.execute("PRAGMA ignore_check_constraints=ON")
        self.conn.execute("UPDATE review_control SET admission=7")
        self.conn.execute("PRAGMA ignore_check_constraints=OFF")
        with self.assertRaisesRegex(ValueError, "cutover_integrity"):
            self.validate(self.conn)

    def test_copy_revalidates_its_own_snapshot_after_prior_validation(
        self,
    ) -> None:
        self.assertEqual(self.validate(self.conn), 1)
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute(
            "INSERT INTO scope_path VALUES('/missing',999,'git')"
        )
        target = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(target.close)
        store.initialize_store(target)
        before = tuple(target.iterdump())
        with self.assertRaisesRegex(ValueError, "cutover_foreign_keys"):
            store_copy.copy_governed_state(self.conn, target)
        self.assertEqual(tuple(target.iterdump()), before)
        self.assertFalse(self.conn.in_transaction)
        self.assertFalse(target.in_transaction)
