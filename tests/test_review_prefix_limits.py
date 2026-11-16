"""Read lines before a window without charging the read allowances."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from muninn import review_evidence, review_originals
from muninn.review_read_budget import ReadBudget
from muninn.review_selectors import MAX_RECORDS, MAX_SCAN_BYTES
from tests import test_review_evidence as evidence_tests
from tests import test_review_window_boundary as boundary_tests
from tests.ingest_support import line

WINDOW = b'{"type":"window"}\n'


class PrefixLimitTests(unittest.TestCase):
    """Place one window line immediately after each allowance."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.dict(
            os.environ,
            {"MUNINN_ROOTS": json.dumps({"claude-projects": tmp.name})},
        )
        patch.start()
        self.addCleanup(patch.stop)

    def read(self, prefix: list[bytes], max_bytes: int) -> ReadBudget:
        """Read only the window line after the prefix and check it."""
        with (self.root / "s.jsonl").open("wb") as handle:
            handle.writelines(prefix)
            handle.write(WINDOW)
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            source = conn.execute(
                "SELECT 'claude' AS provider,'claude-projects' AS root,"
                "'s.jsonl' AS path"
            ).fetchone()
            budget = ReadBudget(max_bytes)
            number = len(prefix) + 1
            records = review_originals.originals(
                source,
                number,
                number,
                max_bytes,
                require_cursor=False,
                budget=budget,
            )
        self.assertEqual([(r[0], r[1]) for r in records], [(number, WINDOW)])
        self.assertEqual(budget.records_left, MAX_RECORDS - 1)
        self.assertEqual(
            budget.bytes_left, min(max_bytes, MAX_SCAN_BYTES) - len(WINDOW)
        )
        return budget

    def test_window_after_one_mebibyte_prefix(self) -> None:
        record = b"{" + b" " * (600 * 1024) + b"}\n"
        self.read([record, record], 1024 * 1024)

    def test_window_after_record_limit(self) -> None:
        self.read([b"{}\n"] * MAX_RECORDS, 1024 * 1024)

    def test_window_after_scan_byte_limit(self) -> None:
        record = b"{" + b" " * (7 * 1024 * 1024) + b"}\n"
        self.read([record] * 5, MAX_SCAN_BYTES)


class PrefixPositionTests(unittest.TestCase):
    """Charge the same window bytes wherever the window starts."""

    def setUp(self) -> None:
        self.case = evidence_tests.ReviewEvidenceTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.boundary = boundary_tests.WindowBoundaryTests()
        self.boundary.case = self.case

    def charged(self, number: int, raw: bytes) -> tuple[int, int]:
        """Validate a one-line window and return the bytes and records."""
        window = self.boundary.window(number, raw)
        window = window.model_copy(update={"from_line": number})
        budget = ReadBudget(100_000)
        review_evidence.validate_window_boundary(
            self.case.conn, window, 100_000, budget=budget
        )
        return 100_000 - budget.bytes_left, MAX_RECORDS - budget.records_left

    def test_window_position_does_not_change_charge(self) -> None:
        raw = b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
        self.case.path.write_bytes(self.case.path.read_bytes() + raw)
        self.case.run_ingest()
        near = self.charged(6, raw)
        self.assertEqual(near, (len(raw), 1))
        # Each window ends the file so block reads after it are equal.
        padding = line(self.case.records[-1])
        self.case.path.write_bytes(
            self.case.path.read_bytes() + padding * MAX_RECORDS + raw
        )
        self.case.run_ingest()
        self.assertEqual(self.charged(MAX_RECORDS + 7, raw), near)


if __name__ == "__main__":
    unittest.main()
