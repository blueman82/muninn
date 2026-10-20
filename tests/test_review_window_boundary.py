"""Validate physical window boundaries without requiring selected events."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

from muninn import cursor_import, review_evidence, review_originals
from muninn.review_evidence_models import EvidenceWindow, SourceRef
from muninn.review_read_budget import ReadBudget
from tests import test_review_evidence as evidence_tests
from tests.ingest_support import line
from tests.test_classify import reply


class WindowBoundaryTests(unittest.TestCase):
    """Use disposable originals and a caller-owned store transaction."""

    def setUp(self) -> None:
        self.case = evidence_tests.ReviewEvidenceTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def window(self, number: int, raw: bytes) -> EvidenceWindow:
        """Bind a window to independently hashed physical bytes."""
        return EvidenceWindow(
            source=self.case.source_ref,
            from_line=1,
            through_line=number,
            boundary_hash=hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest(),
        )

    def validate(self, window: EvidenceWindow, limit: int = 100_000) -> None:
        """Call the public boundary API using the caller connection."""
        review_evidence.validate_window_boundary(self.case.conn, window, limit)

    def metadata(self) -> tuple[EvidenceWindow, bytes]:
        """Append and ingest a complete record with no indexed event."""
        raw = b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
        self.case.path.write_bytes(self.case.path.read_bytes() + raw)
        self.case.run_ingest()
        return self.window(6, raw), raw

    def native(self) -> tuple[Path, EvidenceWindow, list[bytes]]:
        """Import skipped, blank and visible native bubbles in header order."""
        path = self.case.tmp / "boundary.sqlite"
        values = [
            b'{"fullConversationHeadersOnly":[{"bubbleId":"skip"},'
            b'{"bubbleId":"blank"},{"bubbleId":"last"}]}',
            b'{"type":3,"text":"hidden"}',
            b'{"type":1,"text":"   "}',
            b'{"type":1,"text":"Visible"}',
        ]
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE cursorDiskKV(key TEXT,value BLOB)")
            conn.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                zip(
                    (
                        "composerData:chat",
                        "bubbleId:chat:skip",
                        "bubbleId:chat:blank",
                        "bubbleId:chat:last",
                    ),
                    values,
                    strict=True,
                ),
            )
        cursor_import.run(self.case.home, path, str(self.case.tmp), 0.0)
        window = EvidenceWindow(
            source=SourceRef(provider="cursor", thread_id="chat"),
            from_line=1,
            through_line=2,
            boundary_hash=hashlib.sha256(values[-1]).hexdigest(),
        )
        return path, window, values

    def test_complete_nonevent_boundary_stays_valid_after_append(self) -> None:
        window, _ = self.metadata()
        self.assertIsNone(
            self.case.conn.execute(
                "SELECT 1 FROM event WHERE line=6"
            ).fetchone()
        )
        self.validate(window)
        self.case.path.write_bytes(
            self.case.path.read_bytes() + line(reply(5, "Later"))
        )
        self.case.run_ingest()
        before = self.case.path.read_bytes()
        self.validate(window)
        self.assertEqual(self.case.path.read_bytes(), before)

    def test_complete_boundary_does_not_depend_on_cursor_or_events(
        self,
    ) -> None:
        window, _ = self.metadata()
        self.case.conn.execute("DELETE FROM event")
        self.case.conn.execute("UPDATE source SET cursor_line=1")
        self.validate(window)

    def test_changed_missing_incomplete_and_unavailable_refused(self) -> None:
        window, raw = self.metadata()
        before = self.case.path.read_bytes()
        for changed in (
            before.replace(b"token_count", b"other_count"),
            before[: -len(raw)],
            before[:-1],
        ):
            with self.subTest(changed=changed[-30:]):
                self.case.path.write_bytes(changed)
                with self.assertRaises(ValueError):
                    self.validate(window)
        self.case.path.unlink()
        with self.assertRaises(ValueError):
            self.validate(window)

    def test_invalid_budgets_refused(self) -> None:
        window, _ = self.metadata()
        for limit in (0, -1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.validate(window, limit)

    def test_traversal_bytes_are_charged_and_reads_never_exceed_cap(
        self,
    ) -> None:
        window, raw = self.metadata()
        self.case.conn.execute("DELETE FROM event")
        before = self.case.path.read_bytes()
        cap = len(before) - 1
        consumed: list[int] = []

        class Observed(io.BytesIO):
            """Record actual bytes returned by each bounded read."""

            def read1(self, size: int | None = -1) -> bytes:
                assert size is not None
                self_outer.assertGreater(size, 0)
                data = super().read1(size)
                consumed.append(len(data))
                self_outer.assertLessEqual(sum(consumed), cap)
                return data

        self_outer = self
        with (
            mock.patch(
                "muninn.review_originals.open_regular",
                return_value=Observed(before),
            ),
            self.assertRaises(ValueError),
        ):
            self.validate(window, cap)
        self.assertGreater(sum(consumed), len(raw))
        self.validate(window, len(before))

    def test_indexed_anchor_does_not_avoid_prefix_budget(self) -> None:
        window, raw = self.metadata()
        anchor = line(self.case.records[-1])
        self.validate(window, len(self.case.path.read_bytes()))
        with self.assertRaises(ValueError):
            self.validate(window, len(anchor) + len(raw) - 1)

    def test_record_scan_and_count_ceilings_remain_enforced(self) -> None:
        window, _ = self.metadata()
        self.case.conn.execute("DELETE FROM event")
        for constant, value in (
            ("MAX_RECORD_BYTES", 8),
            ("MAX_SCAN_BYTES", 8),
            ("MAX_RECORDS", 2),
        ):
            with (
                mock.patch(f"muninn.review_originals.{constant}", value),
                self.subTest(constant=constant),
                self.assertRaises(ValueError),
            ):
                self.validate(window)

    def test_tombstone_and_unknown_ancestor_refused(self) -> None:
        window, _ = self.metadata()
        self.case.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,line_sha256,"
            "created_at) VALUES('codex','line',?,6,?,0)",
            (window.source.thread_id, window.boundary_hash),
        )
        with self.assertRaisesRegex(ValueError, "evidence_erased"):
            self.validate(window)
        self.case.conn.execute("DELETE FROM tombstone")
        self.case.conn.execute("UPDATE source SET forked_from_id='unknown'")
        with self.assertRaisesRegex(ValueError, "ancestry_unavailable"):
            self.validate(window)

    def test_inactive_and_nonprimary_sources_refused(self) -> None:
        window, _ = self.metadata()
        for column, value in (
            ("status", "missing"),
            ("thread_class", "subagent"),
        ):
            self.case.conn.execute(f"UPDATE source SET {column}=?", (value,))
            with self.assertRaises(ValueError):
                self.validate(window)
            self.case.conn.execute(
                "UPDATE source SET status='active',thread_class='primary'"
            )

    def test_caller_transaction_and_bytes_remain_unchanged(self) -> None:
        window, _ = self.metadata()
        before = self.case.path.read_bytes()
        statements: list[str] = []
        self.case.conn.execute("BEGIN IMMEDIATE")
        self.case.conn.set_trace_callback(statements.append)
        try:
            self.validate(window)
            with self.assertRaises(ValueError):
                self.validate(
                    window.model_copy(update={"boundary_hash": "0" * 64})
                )
            self.assertTrue(self.case.conn.in_transaction)
        finally:
            self.case.conn.set_trace_callback(None)
            self.case.conn.rollback()
        self.assertFalse(
            any(
                s.split()[0].upper()
                in {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT"}
                for s in statements
            )
        )
        self.assertEqual(self.case.path.read_bytes(), before)

    def test_native_materialization_hash_and_readonly_bytes(self) -> None:
        path, window, values = self.native()
        before = path.read_bytes()
        self.validate(window)
        self.validate(
            window.model_copy(
                update={
                    "through_line": 1,
                    "boundary_hash": hashlib.sha256(values[2]).hexdigest(),
                }
            )
        )
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.validate(
                window.model_copy(
                    update={
                        "boundary_hash": hashlib.sha256(values[1]).hexdigest()
                    }
                )
            )
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "UPDATE cursorDiskKV SET value=? "
                "WHERE key='bubbleId:chat:last'",
                (b'{"type":1,"text":"changed"}',),
            )
        with self.assertRaises(ValueError):
            self.validate(window)

    def test_native_metadata_and_skipped_cells_consume_budget(self) -> None:
        _, window, values = self.native()
        keys = (
            "bubbleId:chat:skip",
            "bubbleId:chat:blank",
            "bubbleId:chat:last",
        )
        total = sum(map(len, values)) + sum(len(key.encode()) for key in keys)
        self.validate(window, total)
        for cap in (len(values[-1]), len(values[0]) - 1, total - 1):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                self.validate(window, cap)

    def test_native_incomplete_missing_and_structural_limit_refused(
        self,
    ) -> None:
        path, window, _ = self.native()
        with (
            mock.patch("muninn.review_originals.MAX_RECORDS", 2),
            self.assertRaises(ValueError),
        ):
            self.validate(window)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "UPDATE cursorDiskKV SET value=? "
                "WHERE key='bubbleId:chat:last'",
                (b'{"type":1,',),
            )
        with self.assertRaises(ValueError):
            self.validate(window)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "DELETE FROM cursorDiskKV WHERE key='bubbleId:chat:last'"
            )
        with self.assertRaises(ValueError):
            self.validate(window)

    def test_native_returned_bytes_never_exceed_cap(self) -> None:
        path, window, values = self.native()
        original = review_originals._cursor_connection
        cap = len(values[0]) + 1
        consumed: list[int] = []

        def counted(
            cursor: sqlite3.Cursor, row: tuple[object, ...]
        ) -> tuple[object, ...]:
            consumed.extend(
                len(value.encode()) if isinstance(value, str) else len(value)
                for value in row
                if isinstance(value, (str, bytes))
            )
            self.assertLessEqual(sum(consumed), cap)
            return row

        def connection(
            stack: ExitStack, database: Path, *, budget: ReadBudget
        ) -> sqlite3.Connection:
            conn = original(stack, database, budget=budget)
            conn.row_factory = counted
            return conn

        with mock.patch(
            "muninn.review_originals._cursor_connection",
            side_effect=connection,
        ):
            with self.assertRaises(ValueError):
                self.validate(window, cap)
            self.assertEqual(sum(consumed), len(values[0]))
            consumed.clear()
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    "INSERT INTO cursorDiskKV VALUES('composerData:chat',?)",
                    (values[0],),
                )
            with self.assertRaises(ValueError):
                self.validate(window, cap)

    def test_jsonl_os_read_does_not_prefetch_beyond_budget(self) -> None:
        window, _ = self.metadata()
        self.case.conn.execute("DELETE FROM event")
        original = review_originals.open_regular
        offsets: list[int] = []

        def observed(
            path: Path, *, root: Path | None = None
        ) -> io.BufferedReader:
            handle = original(path, root=root)
            assert isinstance(handle, io.BufferedReader)
            raw = handle.raw
            assert isinstance(raw, io.FileIO)
            raw_read = raw.readinto

            def readinto(buffer: bytearray) -> int | None:
                result = raw_read(buffer)
                offsets.append(raw.tell())
                self.assertLessEqual(raw.tell(), 1)
                return result

            patch = mock.patch.object(raw, "readinto", side_effect=readinto)
            patch.start()
            self.addCleanup(patch.stop)
            return handle

        with (
            mock.patch(
                "muninn.review_originals.open_regular", side_effect=observed
            ),
            self.assertRaises(ValueError),
        ):
            self.validate(window, 1)
        self.assertTrue(offsets)

    def test_existing_selection_keeps_its_cursor_limit(self) -> None:
        self.case.conn.execute("UPDATE source SET cursor_line=1")
        with self.assertRaises(ValueError):
            self.case.select(2, "/payload/content/0/text")

    def test_same_size_prefix_rewrite_cannot_relocate_boundary(self) -> None:
        window, raw = self.metadata()
        before = self.case.path.read_bytes()
        records = before.splitlines(keepends=True)
        size = len(records[0]) + len(records[1])
        replacement = records[0].rstrip(b"\n").ljust(size - 1, b" ") + b"\n"
        changed = replacement + b"".join(records[2:])
        self.assertEqual(len(changed), len(before))
        self.assertEqual(len(changed.splitlines()), 5)
        self.assertTrue(
            all(isinstance(json.loads(r), dict) for r in changed.splitlines())
        )
        self.assertEqual(changed.splitlines(keepends=True)[-1], raw)
        self.case.path.write_bytes(changed)
        with self.assertRaises(ValueError):
            self.validate(window)
        self.assertEqual(self.case.path.read_bytes(), changed)

    def test_native_unrelated_rows_hit_sql_work_ceiling(self) -> None:
        path, window, values = self.native()
        cap = sum(map(len, values)) + sum(
            map(
                len,
                (
                    "bubbleId:chat:skip",
                    "bubbleId:chat:blank",
                    "bubbleId:chat:last",
                ),
            )
        )
        self.validate(window, cap)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                ((f"unrelated:{i}", b"x") for i in range(10_000)),
            )
        before = path.read_bytes()
        self.case.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(ValueError) as caught:
                self.validate(window, cap)
            cause = caught.exception.__cause__
            assert isinstance(cause, sqlite3.OperationalError)
            self.assertEqual(cause.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
            self.assertTrue(self.case.conn.in_transaction)
        finally:
            self.case.conn.rollback()
        self.assertEqual(path.read_bytes(), before)
