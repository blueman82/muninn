"""Test versioned legacy recognition and its safe copy boundary."""

from __future__ import annotations

import sqlite3
import unittest

from muninn import store, store_copy
from tests.knowledge_cutover_support import legacy_schema


class LegacyCopyTests(unittest.TestCase):
    """Accept DDL presentation drift but reject unknown or lost columns."""

    def setUp(self) -> None:
        self.target = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.target.close)
        self.target.row_factory = sqlite3.Row
        self.target.execute("PRAGMA foreign_keys=ON")
        store.initialize_store(self.target)

    def source(self, sql: str | None = None) -> sqlite3.Connection:
        """Build one independent schema v3 fixture."""
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.executescript(sql or legacy_schema())
        return conn

    def test_ddl_presentation_does_not_define_legacy_format(self) -> None:
        sql = legacy_schema().replace(
            "CREATE TABLE scope (", 'CREATE TABLE "scope" ('
        )
        source = self.source(sql)
        self.assertEqual(store_copy.validate_source(source), 3)
        store_copy.copy_governed_state(source, self.target)
        self.assertEqual(
            self.target.execute("PRAGMA user_version").fetchone()[0], 1
        )

    def test_unknown_table_and_extra_column_refuse_copy(self) -> None:
        for sql, alter in (
            (None, "CREATE TABLE surprise(value TEXT)"),
            (None, "ALTER TABLE scope ADD COLUMN unexpected TEXT"),
        ):
            with self.subTest(alter=alter):
                source = self.source(sql)
                source.execute(alter)
                with self.assertRaisesRegex(
                    ValueError, "cutover_source_mismatch"
                ):
                    store_copy.copy_governed_state(source, self.target)
                self.assertEqual(
                    self.target.execute(
                        "SELECT count(*) FROM event"
                    ).fetchone()[0],
                    0,
                )
