"""Store schema contract: tables, constraints, pragmas and private modes.

Every test uses temp dirs only; nothing here touches a live data dir.
"""

from __future__ import annotations

import os
import sqlite3
import unittest

from muninn import store
from tests.store_support import (
    StoreCase,
    insert_event,
    insert_scope,
    insert_source,
    mode,
)


class SchemaTests(StoreCase):
    """Schema version 1 layout, pragmas, constraints and FTS triggers."""

    def test_connect_rw_creates_schema_version_1(self) -> None:
        conn = self.rw()
        self.assertEqual(store.SCHEMA_VERSION, 1)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        found = {
            (r["type"], r["name"])
            for r in conn.execute(
                "SELECT type, name FROM sqlite_master"
                " WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            )
        }
        tables = {
            "scope",
            "scope_path",
            "source",
            "source_issue",
            "event",
            "event_fts",
            "knowledge",
            "knowledge_fts",
            "citation",
            "knowledge_log",
            "tombstone",
        }
        indexes = {
            "source_session",
            "event_scope_ts",
            "event_cwd",
            "citation_target",
            "tombstone_session",
            "tombstone_thread",
        }
        triggers = {
            "event_ai",
            "event_ad",
            "event_immutable",
            "knowledge_ai",
            "knowledge_ad",
            "knowledge_au",
            "knowledge_log_no_update",
            "knowledge_log_no_delete",
        }
        self.assertLessEqual({("table", t) for t in tables}, found)
        self.assertEqual({n for t, n in found if t == "index"}, indexes)
        self.assertEqual({n for t, n in found if t == "trigger"}, triggers)

    def test_connect_rw_reopen_keeps_data(self) -> None:
        first = self.rw()
        insert_scope(first, "/keep")
        first.close()
        again = self.rw()
        self.assertEqual(again.execute("PRAGMA user_version").fetchone()[0], 1)
        keys = [r["key"] for r in again.execute("SELECT key FROM scope")]
        self.assertEqual(keys, ["/keep"])

    def test_failed_schema_creation_rolls_back(self) -> None:
        store.ensure_private_dir(self.home)
        raw = sqlite3.connect(self.db)
        raw.execute("CREATE TABLE tombstone (x)")  # collides with the last
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            store.connect_rw(self.db)
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        tables = [
            r[0]
            for r in raw.execute("SELECT name FROM sqlite_master")
            if r[0] != "sqlite_sequence"
        ]
        self.assertEqual(tables, ["tombstone"])
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_connect_rw_refuses_newer_schema(self) -> None:
        store.ensure_private_dir(self.home)
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA user_version=2")
        raw.close()
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_rw(self.db)

    def test_journal_mode_is_delete(self) -> None:
        conn = self.rw()
        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0], "delete"
        )
        insert_scope(conn)
        self.assertEqual(
            sorted(p.name for p in self.home.iterdir()), ["muninn.sqlite"]
        )  # no -wal, -shm or leftover -journal after a write

    def test_connect_rw_refuses_wal_mode(self) -> None:
        store.ensure_private_dir(self.home)
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("CREATE TABLE x (y)")
        raw.commit()
        raw.close()
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_rw(self.db)

    def test_writer_pragmas(self) -> None:
        conn = self.rw()

        def pragma(name: str) -> object:
            return conn.execute(f"PRAGMA {name}").fetchone()[0]

        self.assertEqual(pragma("journal_mode"), "delete")
        self.assertEqual(pragma("secure_delete"), 1)
        self.assertEqual(pragma("foreign_keys"), 1)
        self.assertEqual(pragma("busy_timeout"), 5000)
        self.assertEqual(pragma("synchronous"), 2)
        self.assertEqual(pragma("cache_size"), -262144)
        self.assertEqual(pragma("fullfsync"), 1)

    def test_fullfsync_can_be_disabled(self) -> None:
        conn = self.rw(fullfsync=False)
        self.assertEqual(conn.execute("PRAGMA fullfsync").fetchone()[0], 0)

    def test_private_modes(self) -> None:
        old = os.umask(0o022)  # permissive, so 0600/0700 are ours, not umask
        self.addCleanup(os.umask, old)
        conn = self.rw()
        conn.execute("BEGIN IMMEDIATE")
        insert_scope(conn)
        self.assertEqual(mode(f"{self.db}-journal"), 0o600)
        conn.execute("COMMIT")
        with store.writer_lock(self.home):
            self.assertEqual(mode(self.home / "writer.lock"), 0o600)
        self.assertEqual(mode(self.home), 0o700)
        self.assertEqual(mode(self.db), 0o600)

    def test_lax_existing_db_is_tightened(self) -> None:
        store.ensure_private_dir(self.home)
        self.db.touch()
        self.db.chmod(0o644)
        self.rw()
        self.assertEqual(mode(self.db), 0o600)

    def test_missing_home_is_created_private_by_lock_and_connect(self) -> None:
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        with store.writer_lock(self.tmp / "via-lock", wait_s=0):
            pass
        conn = store.connect_rw(self.tmp / "via-connect" / "muninn.sqlite")
        self.addCleanup(conn.close)
        self.assertEqual(mode(self.tmp / "via-lock"), 0o700)
        self.assertEqual(mode(self.tmp / "via-connect"), 0o700)

    def test_event_cwd_column_and_index(self) -> None:
        conn = self.rw()
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(event)")}
        self.assertEqual(cols["cwd"]["type"], "TEXT")
        self.assertEqual(cols["cwd"]["notnull"], 0)
        index = [
            r["name"] for r in conn.execute("PRAGMA index_info(event_cwd)")
        ]
        self.assertEqual(index, ["cwd"])
        sid, src = insert_scope(conn), insert_source(conn)
        with_cwd = insert_event(conn, src, sid, line=1, cwd="/w/repo")
        no_cwd = insert_event(conn, src, sid, line=2)
        query = "SELECT id FROM event WHERE cwd = ?"
        plan = " ".join(
            r["detail"]
            for r in conn.execute("EXPLAIN QUERY PLAN " + query, ("/w/repo",))
        )
        self.assertIn("event_cwd", plan)
        hits = [r["id"] for r in conn.execute(query, ("/w/repo",))]
        self.assertEqual(hits, [with_cwd])
        row = conn.execute("SELECT cwd FROM event WHERE id = ?", (no_cwd,))
        self.assertIsNone(row.fetchone()["cwd"])

    def test_event_kinds_and_parent_link(self) -> None:
        conn = self.rw()
        sid, src = insert_scope(conn), insert_source(conn)
        call = insert_event(conn, src, sid, line=1, kind="tool_call")
        for line, kind in enumerate(
            ("prompt", "reply", "harness", "delegation", "tool_error"), 2
        ):
            insert_event(conn, src, sid, line=line, kind=kind, parent=call)
        err = conn.execute(
            "SELECT parent_event_id FROM event WHERE kind = 'tool_error'"
        ).fetchone()
        self.assertEqual(err["parent_event_id"], call)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_event(conn, src, sid, line=9, kind="tool_output")

    def test_events_may_come_from_any_thread_class(self) -> None:
        conn = self.rw()
        sid = insert_scope(conn)
        for n, cls in enumerate(("primary", "subagent", "reviewer", "other")):
            src = insert_source(conn, f"t{n}", cls)
            insert_event(conn, src, sid, kind="delegation")
        count = conn.execute("SELECT count(*) FROM event").fetchone()[0]
        self.assertEqual(count, 4)

    def test_foreign_keys_enforced(self) -> None:
        conn = self.rw()
        sid = insert_scope(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_event(conn, 999, sid)

    def test_constraints_reject_invalid_rows(self) -> None:
        conn = self.rw()
        sid, src = insert_scope(conn), insert_source(conn)
        bad = {
            "scope kind": (
                "INSERT INTO scope(key, label, kind) VALUES ('/a', 'a', 'x')"
            ),
            "scope_path method": (
                "INSERT INTO scope_path(cwd, scope_id, method)"
                f" VALUES ('/a', {sid}, 'x')"
            ),
            "source thread_class": ("UPDATE source SET thread_class = 'x'"),
            "source_issue code": (
                "INSERT INTO source_issue(source_id, line, at, code)"
                f" VALUES ({src}, 1, 0, 'x')"
            ),
            "event role": (
                "INSERT INTO event(source_id, line, part, byte_offset,"
                " line_sha256, seq, role, kind, scope_id, text) VALUES"
                f" ({src}, 1, 1, 0, 'h', 1, 'developer', 'prompt', {sid}, 't')"
            ),
            "knowledge text null unless erased": (
                "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
                f" created_at) VALUES ({sid}, 'fact', NULL, 'current', 'u', 0)"
            ),
            "tombstone session without root": (
                "INSERT INTO tombstone(created_at, provider, level)"
                " VALUES (0, 'codex', 'session')"
            ),
            "tombstone line without hash": (
                "INSERT INTO tombstone(created_at, provider, level,"
                " thread_id, line) VALUES (0, 'codex', 'line', 't', 1)"
            ),
        }
        for name, sql in bad.items():
            with self.subTest(name), self.assertRaises(sqlite3.IntegrityError):
                conn.execute(sql)
        conn.execute(  # the same shapes are accepted when well formed
            "INSERT INTO tombstone(created_at, provider, level, session_root)"
            " VALUES (0, 'codex', 'session', 's')"
        )
        conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'fact', NULL, 'erased', 'u', 0)",
            (sid,),
        )

    def test_event_rows_are_immutable(self) -> None:
        conn = self.rw()
        sid, src = insert_scope(conn), insert_source(conn)
        eid = insert_event(conn, src, sid)
        for column in ("text", "cwd", "parent_event_id", "flags"):
            with (
                self.subTest(column),
                self.assertRaisesRegex(
                    sqlite3.IntegrityError, "event is immutable"
                ),
            ):
                conn.execute(
                    f"UPDATE event SET {column} = NULL WHERE id = ?",
                    (eid,),
                )
        conn.execute("DELETE FROM event WHERE id = ?", (eid,))  # erase path

    def test_knowledge_log_append_only(self) -> None:
        conn = self.rw()
        conn.execute(
            "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
            " VALUES (1, 'add', 'user', 0)"
        )
        for sql in (
            "UPDATE knowledge_log SET actor = 'x'",
            "DELETE FROM knowledge_log",
        ):
            with (
                self.subTest(sql),
                self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"),
            ):
                conn.execute(sql)

    def test_fts_secure_delete_enabled(self) -> None:
        conn = self.rw()
        for fts in ("event_fts", "knowledge_fts"):
            with self.subTest(fts):
                row = conn.execute(
                    f"SELECT v FROM {fts}_config WHERE k = 'secure-delete'"
                ).fetchone()
                self.assertEqual(row["v"], 1)

    def test_fts_triggers_keep_indexes_consistent(self) -> None:
        conn = self.rw()

        def hits(fts: str, term: str) -> list[int]:
            return [
                r[0]
                for r in conn.execute(
                    f"SELECT rowid FROM {fts} WHERE {fts} MATCH ?", (term,)
                )
            ]

        def check(fts: str) -> None:
            conn.execute(
                f"INSERT INTO {fts}({fts}, rank) VALUES ('integrity-check', 1)"
            )

        sid, src = insert_scope(conn), insert_source(conn)
        e1 = insert_event(conn, src, sid, line=1, text="alpha beta")
        e2 = insert_event(conn, src, sid, line=2, text="gamma delta")
        self.assertEqual(hits("event_fts", "alpha"), [e1])
        conn.execute("DELETE FROM event WHERE id = ?", (e1,))
        self.assertEqual(hits("event_fts", "alpha"), [])
        self.assertEqual(hits("event_fts", "gamma"), [e2])
        check("event_fts")

        def update(sql: str, *args: int) -> None:
            conn.execute(sql, args)

        kid = conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at)"
            " VALUES (?, 'fact', 'omega sigma', 'current', 'u', 0)",
            (sid,),
        ).lastrowid
        assert kid is not None
        self.assertEqual(hits("knowledge_fts", "omega"), [kid])
        update("UPDATE knowledge SET status = 'superseded' WHERE id = ?", kid)
        self.assertEqual(hits("knowledge_fts", "omega"), [kid])
        update("UPDATE knowledge SET text = 'tau' WHERE id = ?", kid)
        self.assertEqual(hits("knowledge_fts", "omega"), [])
        self.assertEqual(hits("knowledge_fts", "tau"), [kid])
        update(
            "UPDATE knowledge SET text = NULL, status = 'erased' WHERE id = ?",
            kid,
        )
        self.assertEqual(hits("knowledge_fts", "tau"), [])
        # touching or deleting an already-erased row must not corrupt FTS
        update("UPDATE knowledge SET retract_reason = NULL WHERE id = ?", kid)
        update("DELETE FROM knowledge WHERE id = ?", kid)
        check("knowledge_fts")


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


if __name__ == "__main__":
    unittest.main()
