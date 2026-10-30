"""Check transcript columns and counts used by ingestion."""

from __future__ import annotations

import sqlite3

from tests.store_support import StoreCase, insert_source


class IngestSchemaTests(StoreCase):
    """Columns and tables that ingest relies on."""

    def test_source_parse_state_column(self) -> None:
        conn = self.rw()
        col = {r["name"]: r for r in conn.execute("PRAGMA table_info(source)")}
        state = col["parse_state"]
        self.assertEqual((state["type"], state["notnull"]), ("TEXT", 0))
        self.assertIsNone(state["dflt_value"])
        src = insert_source(conn)
        row = conn.execute(
            "SELECT parse_state FROM source WHERE id = ?", (src,)
        )
        self.assertIsNone(row.fetchone()["parse_state"])

    def test_usage_table_counts_per_source_and_cascades(self) -> None:
        conn = self.rw()
        cols = [
            (r["name"], r["type"], r["notnull"], r["pk"])
            for r in conn.execute("PRAGMA table_info(usage)")
        ]
        self.assertEqual(
            cols,
            [
                ("source_id", "INTEGER", 0, 1),
                ("provider", "TEXT", 1, 0),
                ("session_root", "TEXT", 1, 0),
                ("calls", "INTEGER", 1, 0),
                ("errors", "INTEGER", 1, 0),
                ("last_ts", "TEXT", 0, 0),
            ],
        )
        src, other = insert_source(conn, "t1"), insert_source(conn, "t2")
        conn.execute(
            "INSERT INTO usage(source_id, provider, session_root)"
            " VALUES (?, 'codex', 't1')",
            (src,),
        )
        row = conn.execute("SELECT calls, errors, last_ts FROM usage")
        self.assertEqual(tuple(row.fetchone()), (0, 0, None))
        with self.assertRaises(sqlite3.IntegrityError):  # provider CHECK
            conn.execute(
                "INSERT INTO usage(source_id, provider, session_root)"
                " VALUES (?, 'gpt', 't2')",
                (other,),
            )
        with self.assertRaises(sqlite3.IntegrityError):  # needs a source
            conn.execute(
                "INSERT INTO usage(source_id, provider, session_root)"
                " VALUES (999, 'codex', 'x')"
            )
        conn.execute("DELETE FROM source WHERE id = ?", (src,))
        left = conn.execute("SELECT count(*) FROM usage").fetchone()[0]
        self.assertEqual(left, 0)  # erasing a source row drops its counts
