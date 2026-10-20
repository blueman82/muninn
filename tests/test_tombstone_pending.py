"""Pending erasure guards use synthetic journals and temporary stores only."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from muninn import store, tombstones
from muninn.tombstone_key import TOMBSTONE_FILE


class PendingTombstoneTests(unittest.TestCase):
    """The foreground guard checks intent without owning erasure or writes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / TOMBSTONE_FILE
        self.conn = store.connect_rw(store.db_path(self.home))
        self.addCleanup(self.conn.close)
        self.row: dict[str, object] = {
            "provider": "codex",
            "level": "session",
            "session_root": "synthetic-root",
            "thread_id": None,
            "line": None,
            "line_sha256": None,
            "created_at": 10,
        }

    def journal(self, *rows: dict[str, object]) -> None:
        """Write only synthetic journal rows into the temporary home."""
        self.path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        self.path.chmod(0o600)

    def insert(self, row: dict[str, object]) -> None:
        """Insert a synthetic row with the complete tombstone schema."""
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,session_root,thread_id,line,"
            "line_sha256,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(
                row.get(key)
                for key in (
                    "provider",
                    "level",
                    "session_root",
                    "thread_id",
                    "line",
                    "line_sha256",
                    "created_at",
                )
            ),
        )

    def pending(self) -> None:
        """Require the plain fixed refusal, without content-bearing details."""
        with self.assertRaises(ValueError) as caught:
            tombstones.require_current_tombstones(self.conn, self.home)
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, ("erasure_pending",))

    def read_only(self, *, pending: bool) -> None:
        """Prove a guard has no write, transaction, replay or log effects."""
        before = {
            str(path.relative_to(self.home)): (
                path.read_bytes() if path.is_file() else None
            )
            for path in self.home.rglob("*")
        }
        database = list(self.conn.iterdump())
        changes = self.conn.total_changes
        transaction = self.conn.in_transaction
        stdout, stderr = io.StringIO(), io.StringIO()

        def authorize(
            action: int,
            _arg1: str | None,
            _arg2: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            return (
                sqlite3.SQLITE_OK
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ)
                else sqlite3.SQLITE_DENY
            )

        self.conn.set_authorizer(authorize)
        try:
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertNoLogs(),
                mock.patch.object(tombstones, "reapply_tombstones") as replay,
                mock.patch.object(tombstones, "replay_support") as scrub,
                mock.patch.object(
                    tombstones, "erase_attempt_material"
                ) as cleanup,
            ):
                if pending:
                    self.pending()
                else:
                    tombstones.require_current_tombstones(self.conn, self.home)
                replay.assert_not_called()
                scrub.assert_not_called()
                cleanup.assert_not_called()
        finally:
            self.conn.set_authorizer(None)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(self.conn.in_transaction, transaction)
        self.assertEqual(list(self.conn.iterdump()), database)
        after = {
            str(path.relative_to(self.home)): (
                path.read_bytes() if path.is_file() else None
            )
            for path in self.home.rglob("*")
        }
        self.assertEqual(after, before)

    def test_absent_journal_passes_without_creating_it(self) -> None:
        self.read_only(pending=False)
        self.assertFalse(self.path.exists())

    def test_empty_journal_passes(self) -> None:
        self.path.write_bytes(b"")
        self.path.chmod(0o600)
        self.read_only(pending=False)

    def test_missing_key_refuses_without_writes_or_content_logs(self) -> None:
        self.journal(self.row)
        self.read_only(pending=True)

    def test_matching_key_passes_with_null_fields(self) -> None:
        self.journal(self.row)
        self.insert(self.row)
        self.read_only(pending=False)

    def test_duplicates_and_timestamps_do_not_create_pending_intent(
        self,
    ) -> None:
        self.insert(self.row | {"created_at": 999})
        self.insert(self.row | {"created_at": 123})
        rows = [
            self.row | {"created_at": value} for value in (10, 0, None, -5)
        ]
        self.journal(*rows, *rows)
        self.read_only(pending=False)

    def test_absent_optional_fields_match_database_nulls(self) -> None:
        self.journal(
            {
                "provider": "codex",
                "level": "session",
                "session_root": "synthetic-root",
            }
        )
        self.insert(self.row)
        self.read_only(pending=False)

    def test_every_identity_field_matters(self) -> None:
        base = self.row | {
            "thread_id": "thread",
            "line": 1,
            "line_sha256": "a" * 64,
        }
        self.insert(base)
        for field, different in (
            ("provider", "claude"),
            ("level", "thread"),
            ("session_root", "other-root"),
            ("thread_id", "other-thread"),
            ("line", 2),
            ("line_sha256", "b" * 64),
        ):
            with self.subTest(field=field):
                self.journal(base | {field: different})
                self.pending()

    def test_existing_database_only_rows_do_not_block(self) -> None:
        self.insert(self.row | {"session_root": "other-root"})
        self.insert(self.row)
        self.journal(self.row)
        self.read_only(pending=False)

    def test_matching_rows_do_not_hide_later_pending_intent(self) -> None:
        self.insert(self.row)
        self.journal(self.row, self.row | {"session_root": "other-root"})
        self.pending()

    def test_caller_transaction_remains_owned_on_pass_and_refusal(
        self,
    ) -> None:
        self.journal(self.row)
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.conn.execute("BEGIN IMMEDIATE")
                self.insert(self.row | {"session_root": "caller-row"})
                if not pending:
                    self.insert(self.row)
                self.read_only(pending=pending)
                self.conn.execute("ROLLBACK")
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM tombstone"
                    ).fetchone()[0],
                    0,
                )

    def test_read_only_sqlite_connection_is_supported(self) -> None:
        self.insert(self.row)
        self.journal(self.row)
        uri = store.db_path(self.home).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            tombstones.require_current_tombstones(conn, self.home)
            self.journal(self.row | {"session_root": "missing"})
            with self.assertRaisesRegex(ValueError, "^erasure_pending$"):
                tombstones.require_current_tombstones(conn, self.home)
        finally:
            conn.close()

    def test_malformed_invalid_and_incomplete_rows_are_refused(self) -> None:
        invalid = (
            "not json",
            "[]",
            "null",
            "{}",
            json.dumps(self.row | {"provider": "unsupported"}),
            json.dumps(self.row | {"level": "line", "line": True}),
            json.dumps(self.row | {"created_at": True}),
            json.dumps(self.row | {"session_root": ""}),
            json.dumps(self.row | {"extra": "SYNTHETIC-CONTENT-CANARY"}),
            '{"provider": "codex", "level": "session",',
        )
        for value in invalid:
            with self.subTest(value=value):
                self.path.write_text(value + "\n", encoding="utf-8")
                self.path.chmod(0o600)
                with self.assertRaisesRegex(
                    ValueError, "^erasure_intent_invalid$"
                ):
                    tombstones.require_current_tombstones(self.conn, self.home)
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM tombstone"
                    ).fetchone()[0],
                    0,
                )

    def test_final_object_without_newline_refuses_even_with_table_key(
        self,
    ) -> None:
        self.insert(self.row)
        self.path.write_text(json.dumps(self.row), encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "^erasure_intent_invalid$"):
            tombstones.require_current_tombstones(self.conn, self.home)

    def test_damaged_trailing_record_never_reports_completion(self) -> None:
        self.insert(self.row)
        self.path.write_text(
            json.dumps(self.row) + '\n{"provider":', encoding="utf-8"
        )
        self.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "^erasure_intent_invalid$"):
            tombstones.require_current_tombstones(self.conn, self.home)

    def test_invalid_utf8_refuses_instead_of_replacement(self) -> None:
        self.path.write_bytes(
            b"\xff\r\n" + json.dumps(self.row).encode() + b"\n"
        )
        self.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "^erasure_intent_invalid$"):
            tombstones.require_current_tombstones(self.conn, self.home)

    def test_directory_journal_raises_actual_read_error(self) -> None:
        self.path.mkdir()
        with self.assertRaises(IsADirectoryError):
            tombstones.require_current_tombstones(self.conn, self.home)

    def test_invalid_parent_raises_actual_io_error(self) -> None:
        parent = self.home / "not-a-directory"
        parent.write_bytes(b"synthetic")
        with self.assertRaises(NotADirectoryError):
            tombstones.require_current_tombstones(self.conn, parent)

    def test_windows_missing_child_under_file_never_means_absent(self) -> None:
        parent = self.home / "invalid-parent"
        parent.write_bytes(b"synthetic")
        journal = parent / TOMBSTONE_FILE
        original = Path.lstat
        before = self.conn.total_changes

        def missing(path: Path) -> os.stat_result:
            if path == journal:
                raise FileNotFoundError("Windows invalid-parent result")
            return original(path)

        for operation in (
            tombstones.require_current_tombstones,
            tombstones.reapply_tombstones,
        ):
            with (
                self.subTest(operation=operation.__name__),
                mock.patch.object(Path, "lstat", missing),
                self.assertRaises(NotADirectoryError),
            ):
                operation(self.conn, parent)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(parent.read_bytes(), b"synthetic")

    def test_missing_home_cannot_certify_an_absent_journal(self) -> None:
        missing = self.home / "missing-parent"
        with self.assertRaises(FileNotFoundError):
            tombstones.require_current_tombstones(self.conn, missing)
        self.assertFalse(missing.exists())

    def test_actual_sql_failure_propagates(self) -> None:
        self.journal(self.row)
        self.conn.execute("DROP TABLE tombstone")
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
            tombstones.require_current_tombstones(self.conn, self.home)

    def test_actual_parser_failure_propagates(self) -> None:
        self.path.write_text(
            "[" * 20000 + "]" * 20000 + "\n", encoding="utf-8"
        )
        self.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "^erasure_intent_invalid$"):
            tombstones.require_current_tombstones(self.conn, self.home)

    def test_accepted_unhashable_identity_is_not_silently_ignored(
        self,
    ) -> None:
        self.journal(self.row | {"session_root": ["synthetic"]})
        with self.assertRaisesRegex(ValueError, "^erasure_intent_invalid$"):
            tombstones.require_current_tombstones(self.conn, self.home)
