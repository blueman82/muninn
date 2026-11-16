"""Meter original access across phases, failures and native snapshots."""

from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, cast
from unittest import mock

from muninn import cursor_import, review_evidence, review_originals
from muninn.review_evidence_models import EvidenceRecord, JsonlOriginal
from muninn.review_models import ReviewPolicy
from muninn.review_read_budget import ReadBudget
from tests import test_review_window_boundary as boundary_tests


class ReadBudgetTests(unittest.TestCase):
    """Check nonrefundable counters and deterministic source isolation."""

    def test_positive_integer_construction_and_ceiling(self) -> None:
        for limit in (0, -1, True, 1.0):
            with (
                self.subTest(limit=limit),
                self.assertRaisesRegex(ValueError, "evidence_limit"),
            ):
                ReadBudget(cast(Any, limit))
        budget = ReadBudget(64 * 1024 * 1024)
        self.assertEqual(budget.bytes_left, 32 * 1024 * 1024)
        self.assertEqual(budget.records_left, 4096)
        self.assertEqual(budget.sql_steps_left, 100_000)

    def test_exact_fit_and_refused_consumption_never_refund(self) -> None:
        budget = ReadBudget(3)
        budget.consume_bytes(2)
        with self.assertRaisesRegex(ValueError, "evidence_limit"):
            budget.consume_bytes(2)
        self.assertEqual(budget.bytes_left, 1)
        budget.consume_bytes(1)
        with self.assertRaises(ValueError):
            budget.consume_bytes(1)
        for _ in range(4096):
            budget.consume_record()
        with self.assertRaises(ValueError):
            budget.consume_record()
        self.assertEqual(budget.records_left, 0)
        for _ in range(99_999):
            self.assertEqual(budget.sqlite_progress(), 0)
        self.assertEqual(budget.sqlite_progress(), 1)
        self.assertEqual(budget.sqlite_progress(), 1)
        self.assertEqual(budget.sql_steps_left, 0)

    def test_partition_transfers_remaining_once_without_refunds(self) -> None:
        budget = ReadBudget(11)
        budget.consume_bytes(1)
        budget.consume_record()
        budget.sqlite_progress()
        shares = budget.partition(3)
        self.assertEqual([s.bytes_left for s in shares], [4, 3, 3])
        self.assertEqual(sum(s.records_left for s in shares), 4095)
        self.assertEqual(sum(s.sql_steps_left for s in shares), 99_999)
        self.assertEqual(budget.bytes_left, 0)
        self.assertEqual(budget.records_left, 0)
        self.assertEqual(budget.sql_steps_left, 0)
        for account in (budget, shares[0]):
            with self.assertRaises(ValueError):
                account.partition(1)
        with self.assertRaises(ValueError):
            budget.consume_bytes(1)
        shares[0].consume_bytes(4)
        with self.assertRaises(ValueError):
            shares[0].consume_bytes(1)
        self.assertEqual(shares[1].bytes_left, 3)

    def test_invalid_partition_preserves_funding(self) -> None:
        budget = ReadBudget(2)
        for count in (0, -1, True, 3):
            with self.subTest(count=count), self.assertRaises(ValueError):
                budget.partition(count)
            self.assertEqual(budget.bytes_left, 2)
        self.assertEqual(len(budget.partition(2)), 2)


class SharedReaderTests(unittest.TestCase):
    """Run production readers over disposable physical and native sources."""

    def setUp(self) -> None:
        self.boundary = boundary_tests.WindowBoundaryTests()
        self.boundary.setUp()
        self.addCleanup(self.boundary.doCleanups)
        self.case = self.boundary.case

    def validate_selection(
        self,
        record: EvidenceRecord,
        budget: ReadBudget,
        policy: ReviewPolicy | None = None,
    ) -> tuple[EvidenceRecord, ...]:
        """Revalidate a stored selection using the supplied shared account."""
        return review_evidence.validate_evidence(
            self.case.conn,
            self.case.home,
            (record.id,),
            record.scope,
            policy if policy is not None else self.case.policy(record),
            budget=budget,
        )

    def test_boundary_and_selection_share_exact_bytes(self) -> None:
        record = self.case.select(2, "/payload/content/0/text")
        self.case.persist(record)
        window, _ = self.boundary.metadata()
        size = len(self.case.path.read_bytes())
        original = record.original_locator
        assert isinstance(original, JsonlOriginal)
        policy = self.case.policy(record)
        before = policy.model_dump_json()
        for cap in (size + original.byte_length, size):
            budget = ReadBudget(cap)
            review_evidence.validate_window_boundary(
                self.case.conn, window, cap, budget=budget
            )
            if cap == size:
                with self.assertRaisesRegex(ValueError, "evidence_limit"):
                    self.validate_selection(record, budget, policy)
            else:
                self.assertEqual(
                    self.validate_selection(record, budget, policy), (record,)
                )
            self.assertEqual(budget.bytes_left, 0)
        self.assertEqual(policy.model_dump_json(), before)

    def test_failed_repeated_selected_reads_remain_charged(self) -> None:
        raw = b"{}\n"
        self.case.path.write_bytes(raw)
        original = JsonlOriginal(
            kind="jsonl", path=self.case.path, byte_offset=0, byte_length=3
        )
        budget = ReadBudget(9)
        with ExitStack() as stack:
            self.assertEqual(
                review_originals.reread(
                    stack, {}, original, 100, budget=budget
                ),
                raw,
            )
            self.case.path.write_bytes(b"{}")
            for _ in range(2):
                self.case.conn.execute("SAVEPOINT probe")
                with self.assertRaisesRegex(
                    ValueError, "original_unavailable"
                ):
                    review_originals.reread(
                        stack, {}, original, 100, budget=budget
                    )
                self.case.conn.execute("ROLLBACK TO probe")
                self.case.conn.execute("RELEASE probe")
        self.assertEqual(budget.bytes_left, 2)
        self.assertEqual(budget.records_left, 4093)

    def test_selected_raw_short_reads_never_prefetch(self) -> None:
        self.case.path.write_bytes(b"{}\n" + b"x" * 20_000)
        original = JsonlOriginal(
            kind="jsonl", path=self.case.path, byte_offset=0, byte_length=3
        )
        fetched: list[int] = []
        open_original = review_originals.open_regular

        def observed(path: Path) -> io.BufferedReader:
            handle = open_original(path)
            assert isinstance(handle, io.BufferedReader)
            raw = handle.raw
            assert isinstance(raw, io.FileIO)
            readinto = raw.readinto

            def short(buffer: bytearray) -> int | None:
                count = readinto(memoryview(buffer)[:1])
                fetched.append(count or 0)
                return count

            patch = mock.patch.object(raw, "readinto", side_effect=short)
            patch.start()
            self.addCleanup(patch.stop)
            return handle

        budget = ReadBudget(3)
        with (
            mock.patch(
                "muninn.review_originals.open_regular", side_effect=observed
            ),
            ExitStack() as stack,
        ):
            self.assertEqual(
                review_originals.reread(stack, {}, original, 3, budget=budget),
                b"{}\n",
            )
        self.assertEqual(fetched, [1, 1, 1])
        self.assertEqual(budget.bytes_left, 0)

    def test_native_records_include_keys_and_skipped_cells(self) -> None:
        _, window, values = self.boundary.native()
        total = sum(map(len, values)) + sum(
            len("bubbleId:chat:" + suffix)
            for suffix in ("skip", "blank", "last")
        )
        budget = ReadBudget(total)
        review_evidence.validate_window_boundary(
            self.case.conn, window, total, budget=budget
        )
        self.assertEqual(budget.bytes_left, 0)
        self.assertEqual(budget.records_left, 4096 - 7)
        limited = ReadBudget(100_000)
        for _ in range(4090):
            limited.consume_record()
        with self.assertRaisesRegex(ValueError, "evidence_limit"):
            review_evidence.validate_window_boundary(
                self.case.conn, window, 100_000, budget=limited
            )
        self.assertEqual(limited.records_left, 0)

    def test_native_work_is_shared_across_connections_and_closes(self) -> None:
        path, _, _ = self.boundary.native()
        baseline = ReadBudget(100_000)
        with ExitStack() as stack:
            conn = review_originals._cursor_connection(
                stack, path, budget=baseline
            )
            review_originals._cell(
                conn, "composerData:chat", 100_000, budget=baseline
            )
        cost = 100_000 - baseline.sql_steps_left
        budget = ReadBudget(100_000)
        for _ in range(100_000 - cost - cost // 2):
            budget.sqlite_progress()
        with ExitStack() as stack:
            first = review_originals._cursor_connection(
                stack, path, budget=budget
            )
            review_originals._cell(
                first, "composerData:chat", 100_000, budget=budget
            )
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")
        other = path.with_name("second.sqlite")
        other.write_bytes(path.read_bytes())
        with ExitStack() as stack:
            second = review_originals._cursor_connection(
                stack, other, budget=budget
            )
            with self.assertRaises(sqlite3.OperationalError):
                review_originals._cell(
                    second, "composerData:chat", 100_000, budget=budget
                )
        self.assertEqual(budget.sql_steps_left, 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            second.execute("SELECT 1")
        with ExitStack() as stack, mock.patch("sqlite3.connect") as connect:
            with self.assertRaisesRegex(ValueError, "evidence_limit"):
                review_originals._cursor_connection(stack, path, budget=budget)
            connect.assert_not_called()

    def test_prefix_charges_tails_and_stops_at_record_limit(self) -> None:
        raw = b"{}\n" * 100
        self.case.path.write_bytes(raw)
        budget = ReadBudget(len(raw))
        fetched: list[int] = []

        class Observed(io.BytesIO):
            """Expose raw fetch lengths without buffering."""

            def read1(self, size: int | None = -1) -> bytes:
                block = super().read1(size)
                fetched.append(len(block))
                return block

        with mock.patch(
            "muninn.review_originals.open_regular", return_value=Observed(raw)
        ):
            review_evidence.validate_window_boundary(
                self.case.conn,
                self.boundary.window(1, b"{}\n"),
                len(raw),
                budget=budget,
            )
        self.assertEqual(fetched, [len(raw)])
        self.assertEqual(budget.bytes_left, 0)
        self.assertEqual(budget.records_left, 4095)
        budget = ReadBudget(len(raw))
        for _ in range(4096):
            budget.consume_record()
        with (
            mock.patch(
                "muninn.review_originals.open_regular",
                return_value=Observed(raw),
            ),
            self.assertRaises(ValueError),
        ):
            review_evidence.validate_window_boundary(
                self.case.conn,
                self.boundary.window(1, b"{}\n"),
                len(raw),
                budget=budget,
            )
        self.assertEqual(fetched, [len(raw)])

    def test_boundary_and_selected_phase_share_record_quota(self) -> None:
        record = self.case.select(2, "/payload/content/0/text")
        self.case.persist(record)
        window, _ = self.boundary.metadata()
        for available in (6, 7):
            budget = ReadBudget(100_000)
            for _ in range(4096 - available):
                budget.consume_record()
            review_evidence.validate_window_boundary(
                self.case.conn, window, 100_000, budget=budget
            )
            if available == 6:
                with self.assertRaisesRegex(ValueError, "evidence_limit"):
                    self.validate_selection(record, budget)
            else:
                self.validate_selection(record, budget)
            self.assertEqual(budget.records_left, 0)

    def test_native_failures_keep_work_and_key_bytes_charged(self) -> None:
        path, window, values = self.boundary.native()
        budget = ReadBudget(len(values[0]) + len("bubbleId:chat:skip"))
        with self.assertRaisesRegex(ValueError, "evidence_limit"):
            review_evidence.validate_window_boundary(
                self.case.conn, window, 100_000, budget=budget
            )
        self.assertEqual(budget.bytes_left, 0)
        self.assertEqual(budget.records_left, 4093)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES('composerData:chat',?)",
                (values[0],),
            )
        budget = ReadBudget(100_000)
        for expected in (4095, 4094):
            with self.assertRaisesRegex(ValueError, "original_unavailable"):
                review_evidence.validate_window_boundary(
                    self.case.conn, window, 100_000, budget=budget
                )
            self.assertEqual(budget.records_left, expected)
            self.assertEqual(budget.bytes_left, 100_000)
        self.assertLess(budget.sql_steps_left, 100_000)
        path.write_bytes(b"not a database")
        with self.assertRaisesRegex(
            ValueError, "original_unavailable"
        ) as caught:
            review_evidence.validate_window_boundary(
                self.case.conn, window, 100_000, budget=budget
            )
        self.assertIsInstance(
            caught.exception.__cause__, sqlite3.DatabaseError
        )

    def test_supplied_budget_does_not_relax_explicit_native_cap(self) -> None:
        _, window, _ = self.boundary.native()
        for cap in (0, 1, True):
            budget = ReadBudget(100_000)
            source = self.case.conn.execute(
                "SELECT * FROM source WHERE provider='cursor'"
            ).fetchone()
            with (
                self.subTest(cap=cap),
                self.assertRaisesRegex(ValueError, "evidence_limit"),
            ):
                review_originals.originals(
                    self.case.conn,
                    source,
                    1,
                    window.through_line,
                    cap,
                    budget=budget,
                )

    def test_utf16_refusal_precedes_original_reads(self) -> None:
        path, window, values = self.boundary.native()
        path.unlink()
        key = "bubbleId:chat:" + "漢" * 100
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("PRAGMA encoding='UTF-16le'")
            conn.execute("CREATE TABLE cursorDiskKV(key TEXT,value BLOB)")
            conn.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                [("composerData:chat", b"{}"), (key, values[-1])],
            )
        cursor_import.run(self.case.home, path, str(self.case.tmp), 0.0)
        window = window.model_copy(update={"through_line": 1})
        before = path.read_bytes()
        fetched: list[int] = []
        connect = sqlite3.connect
        connections: list[sqlite3.Connection] = []

        def counted(
            cursor: sqlite3.Cursor, row: tuple[object, ...]
        ) -> tuple[object, ...]:
            if cursor.description[0][0] == "encoding":
                return row
            fetched.extend(
                len(v.encode("utf-8")) if isinstance(v, str) else len(v)
                for v in row
                if isinstance(v, (str, bytes))
            )
            return row

        def observed(database: str, *, uri: bool) -> sqlite3.Connection:
            conn = connect(database, uri=uri)
            connections.append(conn)
            conn.row_factory = counted
            return conn

        budget = ReadBudget(230)
        with mock.patch(
            "muninn.review_originals.sqlite3.connect", side_effect=observed
        ):
            for _ in range(2):
                steps = budget.sql_steps_left
                with self.assertRaises(ValueError):
                    review_evidence.validate_window_boundary(
                        self.case.conn, window, 230, budget=budget
                    )
                self.assertLessEqual(sum(fetched), 230)
                self.assertEqual(230 - budget.bytes_left, sum(fetched))
                self.assertLess(budget.sql_steps_left, steps)
        self.assertEqual((fetched, budget.records_left), ([], 4096))
        self.assertEqual(path.read_bytes(), before)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
