"""Marked format, review constraints and transcript preservation."""

from __future__ import annotations

import contextlib
import sqlite3
import unittest

from muninn import store
from tests.store_support import StoreCase

# Frozen transcript column shapes guard data copied during the cutover.
TRANSCRIPT_SHAPES = {
    "scope": [
        (0, "id", "INTEGER", 0, None, 1),
        (1, "key", "TEXT", 1, None, 0),
        (2, "label", "TEXT", 1, None, 0),
        (3, "kind", "TEXT", 1, None, 0),
    ],
    "scope_path": [
        (0, "cwd", "TEXT", 0, None, 1),
        (1, "scope_id", "INTEGER", 1, None, 0),
        (2, "method", "TEXT", 1, None, 0),
    ],
    "source": [
        (0, "id", "INTEGER", 0, None, 1),
        (1, "provider", "TEXT", 1, None, 0),
        (2, "thread_id", "TEXT", 1, None, 0),
        (3, "session_root", "TEXT", 1, None, 0),
        (4, "parent_thread_id", "TEXT", 0, None, 0),
        (5, "forked_from_id", "TEXT", 0, None, 0),
        (6, "thread_class", "TEXT", 1, None, 0),
        (7, "class_reason", "TEXT", 1, None, 0),
        (8, "replay_mode", "TEXT", 1, None, 0),
        (9, "replay_before", "INTEGER", 0, None, 0),
        (10, "root", "TEXT", 1, None, 0),
        (11, "path", "TEXT", 1, None, 0),
        (12, "first_line_sha256", "TEXT", 1, None, 0),
        (13, "ino", "INTEGER", 1, None, 0),
        (14, "size", "INTEGER", 1, None, 0),
        (15, "mtime_ns", "INTEGER", 1, None, 0),
        (16, "cursor_bytes", "INTEGER", 1, "0", 0),
        (17, "cursor_line", "INTEGER", 1, "0", 0),
        (18, "anchor_offset", "INTEGER", 0, None, 0),
        (19, "anchor_sha256", "TEXT", 0, None, 0),
        (20, "status", "TEXT", 1, "'active'", 0),
        (21, "skipped_lines", "INTEGER", 1, "0", 0),
        (22, "classifier_version", "INTEGER", 1, None, 0),
        (23, "first_seen", "REAL", 1, None, 0),
        (24, "last_seen", "REAL", 1, None, 0),
        (25, "parse_state", "TEXT", 0, None, 0),
    ],
    "event": [
        (0, "id", "INTEGER", 0, None, 1),
        (1, "source_id", "INTEGER", 1, None, 0),
        (2, "line", "INTEGER", 1, None, 0),
        (3, "part", "INTEGER", 1, None, 0),
        (4, "byte_offset", "INTEGER", 1, None, 0),
        (5, "line_sha256", "TEXT", 1, None, 0),
        (6, "seq", "INTEGER", 1, None, 0),
        (7, "ts", "TEXT", 0, None, 0),
        (8, "role", "TEXT", 1, None, 0),
        (9, "kind", "TEXT", 1, None, 0),
        (10, "tag", "TEXT", 0, None, 0),
        (11, "scope_id", "INTEGER", 1, None, 0),
        (12, "cwd", "TEXT", 0, None, 0),
        (13, "parent_event_id", "INTEGER", 0, None, 0),
        (14, "flags", "INTEGER", 1, "0", 0),
        (15, "text", "TEXT", 1, None, 0),
    ],
    "event_fts": [(0, "text", "", 0, None, 0)],
    "usage": [
        (0, "source_id", "INTEGER", 0, None, 1),
        (1, "provider", "TEXT", 1, None, 0),
        (2, "session_root", "TEXT", 1, None, 0),
        (3, "calls", "INTEGER", 1, "0", 0),
        (4, "errors", "INTEGER", 1, "0", 0),
        (5, "last_ts", "TEXT", 0, None, 0),
    ],
    "source_issue": [
        (0, "source_id", "INTEGER", 1, None, 1),
        (1, "line", "INTEGER", 1, None, 2),
        (2, "at", "REAL", 1, None, 0),
        (3, "code", "TEXT", 1, None, 0),
    ],
    "tombstone": [
        (0, "id", "INTEGER", 0, None, 1),
        (1, "created_at", "REAL", 1, None, 0),
        (2, "provider", "TEXT", 1, None, 0),
        (3, "level", "TEXT", 1, None, 0),
        (4, "session_root", "TEXT", 0, None, 0),
        (5, "thread_id", "TEXT", 0, None, 0),
        (6, "line", "INTEGER", 0, None, 0),
        (7, "line_sha256", "TEXT", 0, None, 0),
    ],
}


class ReviewSchemaTests(StoreCase):
    """Enforce new-format admission and durable knowledge relationships."""

    def request(self, ident: str = "R_one", **overrides: str) -> None:
        """Insert an admitted request with explicit canonical snapshots."""
        row = {
            "id": ident,
            "duplicate_key": ident,
            "provider": "codex",
            "thread_id": "thread",
            "session_root": "thread",
            "from_line": 1,
            "through_line": 2,
            "boundary_hash": "a" * 64,
            "scope_json": '{"project_id":"/repo","task":null}',
            "policy_json": "{}",
            "policy_digest": "b" * 64,
            "trigger": "owner",
            "origin": "review",
            "state": "pending",
            "created_at": 0.0,
        } | overrides
        self.rw().execute(
            f"INSERT INTO review_request ({','.join(row)})"
            f" VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )

    def attempt(self, ident: str, request: str) -> None:
        """Insert a local proposal attempt attached to its request."""
        self.rw().execute(
            "INSERT INTO review_attempt(id,request_id,generation,"
            "observed_json,started_at,progress_at) VALUES(?,?,0,'{}',0,0)",
            (ident, request),
        )

    def receipt(self, request: str, attempt: str) -> None:
        """Insert a successful receipt with empty applied lists."""
        self.rw().execute(
            "INSERT INTO review_receipt(request_id,attempt_id,applied_json,"
            "unresolved_json,committed_at,outcome)"
            " VALUES(?,?,'[]','[]',0,'no_change')",
            (request, attempt),
        )

    def test_marker_and_user_version_required(self) -> None:
        conn = self.rw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(
            tuple(conn.execute("SELECT * FROM store_meta").fetchone()),
            (1, "muninn-knowledge", 2),
        )
        self.assertEqual(
            tuple(conn.execute("SELECT * FROM review_control").fetchone()),
            (1, 0, 0, None, None, None),
        )
        store.assert_store_format(conn)
        conn.execute("PRAGMA user_version=3")
        for opener in (store.connect_ro, store.connect_rw):
            with (
                self.assertRaisesRegex(
                    store.StoreUnavailableError, "^cutover_required$"
                ),
                contextlib.closing(opener(self.db)),
            ):
                pass
        conn.execute("PRAGMA user_version=2")
        conn.execute("DELETE FROM store_meta")
        for opener in (store.connect_ro, store.connect_rw):
            with (
                self.assertRaisesRegex(
                    store.StoreUnavailableError, "^cutover_required$"
                ),
                contextlib.closing(opener(self.db)),
            ):
                pass

    def test_old_format_read_and_write_refused_without_ddl(self) -> None:
        store.ensure_private_dir(self.home)
        for version in (0, 1, 2, 3, 9):
            with self.subTest(version=version):
                self.db.unlink(missing_ok=True)
                with contextlib.closing(sqlite3.connect(self.db)) as old:
                    old.execute("CREATE TABLE retained (text TEXT)")
                    old.execute("INSERT INTO retained VALUES ('untouched')")
                    old.execute(f"PRAGMA user_version={version}")
                    old.commit()
                original = self.db.read_bytes()
                for opener in (store.connect_ro, store.connect_rw):
                    with (
                        self.assertRaisesRegex(
                            store.StoreUnavailableError, "^cutover_required$"
                        ),
                        contextlib.closing(opener(self.db)),
                    ):
                        pass
                    self.assertEqual(self.db.read_bytes(), original)
                    self.assertEqual(
                        sorted(p.name for p in self.home.iterdir()),
                        ["muninn.sqlite"],
                    )
                self.db.unlink()

    def test_wrong_marker_refused_without_ddl(self) -> None:
        store.ensure_private_dir(self.home)
        for marker, version in (("historical", 2), ("muninn-knowledge", 1)):
            with self.subTest(marker=marker, version=version):
                self.db.unlink(missing_ok=True)
                with contextlib.closing(sqlite3.connect(self.db)) as old:
                    old.execute(
                        "CREATE TABLE store_meta(singleton,format,version)"
                    )
                    old.execute(
                        "INSERT INTO store_meta VALUES (1,?,?)",
                        (marker, version),
                    )
                    old.execute("PRAGMA user_version=2")
                    old.commit()
                original = self.db.read_bytes()
                for opener in (store.connect_ro, store.connect_rw):
                    with (
                        self.assertRaisesRegex(
                            store.StoreUnavailableError, "^cutover_required$"
                        ),
                        contextlib.closing(opener(self.db)),
                    ):
                        pass
                    self.assertEqual(self.db.read_bytes(), original)
                self.db.unlink()

    def test_transcript_shapes_unchanged(self) -> None:
        conn = self.rw()
        for table, expected in TRANSCRIPT_SHAPES.items():
            with self.subTest(table=table):
                actual = [
                    tuple(row)
                    for row in conn.execute(f"PRAGMA table_info({table})")
                ]
                self.assertEqual(actual, expected)

    def test_request_policy_snapshot_is_immutable(self) -> None:
        self.request()
        conn = self.rw()
        original = self.db.read_bytes()
        for column in ("policy_json", "policy_digest"):
            with self.subTest(column=column):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(f"UPDATE review_request SET {column}='[]'")
                self.assertEqual(self.db.read_bytes(), original)
        conn.execute(
            "UPDATE review_request SET policy_json=policy_json,"
            "policy_digest=policy_digest,state='running'"
        )
        self.assertEqual(
            conn.execute("SELECT state FROM review_request").fetchone()[0],
            "running",
        )

    def test_unique_request_and_receipt_constraints(self) -> None:
        self.request()
        for overrides in (
            {"duplicate_key": "R_one", "thread_id": "other"},
            {},
        ):
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.request("R_two", **overrides)
        self.request("R_two", thread_id="other")
        self.attempt("A_one", "R_one")
        self.attempt("A_two", "R_one")
        self.receipt("R_one", "A_one")
        for request, attempt in (("R_one", "A_two"), ("R_two", "A_one")):
            with (
                self.subTest(request=request, attempt=attempt),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.receipt(request, attempt)

    def test_foreign_key_and_fts_privacy(self) -> None:
        conn = self.rw()
        with self.assertRaises(sqlite3.IntegrityError):
            self.attempt("A_orphan", "R_absent")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO review_input VALUES ('R_absent','E_absent')"
            )
        conn.execute(
            "INSERT INTO knowledge VALUES ('K_one',1,'active',NULL,NULL)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO knowledge_claim VALUES"
                " ('K_one',1,'note','claim','[]','live')"
            )
        for fts in ("event_fts", "knowledge_fts"):
            with self.subTest(fts=fts):
                self.assertEqual(
                    conn.execute(
                        f"SELECT v FROM {fts}_config WHERE k='secure-delete'"
                    ).fetchone()[0],
                    1,
                )
        self.assertEqual(
            [r[1] for r in conn.execute("PRAGMA table_info(knowledge_fts)")],
            ["knowledge_id", "revision", "text"],
        )
        self.assertEqual(
            " ".join(
                r[1] for r in conn.execute("PRAGMA table_info(review_call)")
            ),
            (
                "attempt_id round_index budget_account_id policy_digest state "
                "reserved_at launched_at ended_at requested_json "
                "observed_json "
                "usage_json cost_reservation_decimal currency "
                "process_identity_json outcome"
            ),
        )

    def test_composite_support_links_and_cascades(self) -> None:
        self.request()
        conn = self.rw()
        conn.execute(
            "INSERT INTO review_evidence VALUES ('E_one','codex','thread',"
            "'thread',1,1,'hash','source','projection','{}','{}','selector',"
            "'owner','owner','{}','excerpt','live',0)"
        )
        conn.execute("INSERT INTO review_input VALUES ('R_one','E_one')")
        conn.execute(
            "INSERT INTO knowledge VALUES ('K_one',1,'active',NULL,NULL)"
        )
        conn.execute(
            "INSERT INTO knowledge_version VALUES ('K_one',1,'R_one','note',"
            "'fact','{}','{}','reason','review',NULL,0,NULL)"
        )
        conn.execute(
            "INSERT INTO knowledge_claim VALUES"
            " ('K_one',1,'note','claim','[]','live')"
        )
        for revision, key, evidence in (
            (2, "note", "E_one"),
            (1, "absent", "E_one"),
            (1, "note", "E_absent"),
        ):
            with (
                self.subTest(revision=revision, key=key, evidence=evidence),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                conn.execute(
                    "INSERT INTO knowledge_support VALUES ('K_one',?,?,?)",
                    (revision, key, evidence),
                )
        conn.execute(
            "INSERT INTO knowledge_support VALUES ('K_one',1,'note','E_one')"
        )
        conn.execute("DELETE FROM knowledge_claim")
        self.assertEqual(
            conn.execute("SELECT count(*) FROM knowledge_support").fetchone()[
                0
            ],
            0,
        )
        conn.execute("DELETE FROM knowledge_version")
        conn.execute("DELETE FROM review_request")
        self.assertEqual(
            conn.execute("SELECT count(*) FROM review_input").fetchone()[0], 0
        )
        self.assertEqual(
            conn.execute("SELECT count(*) FROM review_evidence").fetchone()[0],
            1,
        )

    def test_json_state_and_cursor_constraints(self) -> None:
        conn = self.rw()
        for overrides in (
            {"state": "invented"},
            {"origin": "invented"},
            {"scope_json": "bad"},
            {"policy_json": "bad"},
        ):
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.request(**overrides)
        for cursor in (
            "activation_line",
            "examined_line",
            "eligible_line",
            "completed_line",
        ):
            with (
                self.subTest(cursor=cursor),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                conn.execute(
                    "INSERT INTO review_source(provider,thread_id,"
                    "activation_line,"
                    "examined_line,eligible_line,completed_line,updated_at)"
                    " VALUES ('codex','t',0,0,0,0,0)"
                )
                conn.execute(f"UPDATE review_source SET {cursor}=-1")
            conn.execute("DELETE FROM review_source")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE review_control SET policy_json='bad'")


if __name__ == "__main__":
    unittest.main()
