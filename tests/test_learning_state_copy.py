"""Keep learning choices and repair notes during store replacement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from muninn import (
    review_history,
    review_recovery,
    review_state,
    store,
    store_copy,
)
from tests.test_review_requests import RequestCase


class LearningStateCopyTests(RequestCase):
    """Check both older stores and stores with additive learning state."""

    def test_copy_preserves_the_future_only_choice(self) -> None:
        self.transaction(
            lambda: review_history.configure(
                self.conn, self.policy, "now", None, 20.0, None
            )
        )
        target = sqlite3.connect(":memory:", isolation_level=None)
        target.row_factory = sqlite3.Row
        self.addCleanup(target.close)
        store.initialize_store(target)
        store_copy.copy_governed_state(self.conn, target)
        self.assertEqual(
            review_state.selection(self.conn), review_state.selection(target)
        )

    def test_older_store_remains_a_valid_copy_source(self) -> None:
        self.conn.execute("DROP TABLE learning_setup")
        self.conn.execute("DROP TABLE review_issue")
        with self.assertRaisesRegex(ValueError, "^cutover_source_mismatch$"):
            store_copy.validate_source(self.conn)
        old = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(old.close)
        fixture = (
            Path(__file__).parent / "fixtures/installer/marked-v1-base.sql"
        )
        old.executescript(fixture.read_text())
        self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(
            old.execute("SELECT * FROM store_meta").fetchall(),
            [(1, "muninn-knowledge", 1)],
        )
        self.assertEqual(store_copy.validate_source(old), 1)

    def test_unknown_extension_shape_is_refused(self) -> None:
        self.conn.execute(
            "ALTER TABLE learning_setup ADD COLUMN surprise TEXT"
        )
        with self.assertRaisesRegex(ValueError, "cutover_source_mismatch"):
            store_copy.validate_source(self.conn)

    def test_copy_preserves_saved_failure_notice(self) -> None:
        ident = self.enqueue().id
        self.transaction(
            lambda: review_recovery.record(
                self.conn, ident, "evidence_limit", "evidence", 20.0
            )
        )
        target = sqlite3.connect(":memory:", isolation_level=None)
        target.row_factory = sqlite3.Row
        self.addCleanup(target.close)
        store.initialize_store(target)
        store_copy.copy_governed_state(self.conn, target)
        self.assertEqual(
            review_recovery.notice(self.conn), review_recovery.notice(target)
        )
