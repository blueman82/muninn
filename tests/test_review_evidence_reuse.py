"""Check fresh physical-read reuse without weakening selection verification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from contextlib import closing

from muninn import cursor_import, review_evidence
from muninn.review_evidence_models import (
    CursorOriginal,
    EvidenceLocator,
    EvidenceRecord,
    EvidenceSelector,
    SourceRef,
)
from muninn.review_read_budget import ReadBudget
from tests import test_review_evidence as evidence_tests
from tests.ingest_support import line, rollout
from tests.test_classify import user_msg


class EvidenceReuseTests(unittest.TestCase):
    """Use disposable physical originals and real persisted projections."""

    def setUp(self) -> None:
        self.case = evidence_tests.ReviewEvidenceTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def selections(self, native: bool = False) -> tuple[EvidenceRecord, ...]:
        """Persist two small selections sharing a large physical original."""
        text = "Use Python. Please test."
        if native:
            path = self.case.tmp / "reuse.sqlite"
            raw = json.dumps(
                {"type": 1, "text": text, "metadata": "x" * 16_000}
            ).encode()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE cursorDiskKV(key TEXT,value BLOB)")
                db.executemany(
                    "INSERT INTO cursorDiskKV VALUES(?,?)",
                    [
                        ("composerData:reuse", b"{}"),
                        ("bubbleId:reuse:a", raw),
                    ],
                )
            cursor_import.run(self.case.home, path, str(self.case.tmp), 0.0)
            locator = EvidenceLocator(
                source=SourceRef(provider="cursor", thread_id="reuse"),
                line=1,
                part=1,
                line_sha256=hashlib.sha256(raw).hexdigest(),
            )
            pointer = "/text"
        else:
            self.case.records[1] = user_msg(1, text)
            self.case.records[1]["metadata"] = "x" * 16_000
            self.case.path = self.case.write(rollout(), self.case.records)
            self.case.run_ingest(full=True)
            locator = self.case.locator(2)
            pointer = "/payload/content/0/text"
        records = tuple(
            review_evidence.select_evidence(
                self.case.home,
                locator,
                EvidenceSelector(json_pointer=pointer, start=start, end=end),
            )
            for start, end in ((0, 11), (12, 24))
        )
        for record in records:
            self.case.persist(record)
        return records

    def check(
        self, records: tuple[EvidenceRecord, ...], budget: ReadBudget
    ) -> tuple[EvidenceRecord, ...]:
        """Run one complete validation against the real originals."""
        return review_evidence.validate_evidence(
            self.case.conn,
            self.case.home,
            tuple(r.id for r in records),
            records[0].scope,
            self.case.policy(records[0]),
            budget=budget,
        )

    def test_one_fresh_read_covers_jsonl_and_native_selections(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                records = self.selections(native)
                budget = ReadBudget(17_000)
                self.assertEqual(self.check(records, budget), records)
                self.assertLess(budget.bytes_left, 1000)
                self.assertEqual(budget.records_left, 4095)
                self.assertEqual(
                    [r.excerpt for r in records],
                    ["Use Python.", "Please test."],
                )

    def test_reused_original_checks_each_persisted_selection(self) -> None:
        records = self.selections()
        for column, value, cause in (
            ("line_sha256", "0" * 64, "evidence_changed"),
            (
                "selector_json",
                '{"json_pointer":"/metadata","start":null,"end":null}',
                "forbidden_selector",
            ),
            ("state", "changed", "evidence_unavailable"),
            ("projection_version", "unknown", "evidence_unavailable"),
            (
                "scope_json",
                '{"project_id":"elsewhere","task":null}',
                "evidence_unavailable",
            ),
        ):
            with self.subTest(column=column):
                self.case.conn.execute("SAVEPOINT selection")
                try:
                    self.case.conn.execute(
                        f"UPDATE review_evidence SET {column}=? WHERE id=?",
                        (value, records[1].id),
                    )
                    with self.assertRaisesRegex(ValueError, cause):
                        self.check(records, ReadBudget(17_000))
                finally:
                    self.case.conn.execute("ROLLBACK TO selection")
                    self.case.conn.execute("RELEASE selection")

    def test_later_operation_rereads_changed_original(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                records = self.selections(native)
                self.assertEqual(
                    self.check(records, ReadBudget(17_000)), records
                )
                original = records[0].original_locator
                if isinstance(original, CursorOriginal):
                    with closing(sqlite3.connect(original.database)) as db, db:
                        db.execute(
                            "UPDATE cursorDiskKV SET value=replace("
                            "CAST(value AS TEXT),"
                            "'Use Python.','Use Erlang.') WHERE key=?",
                            (original.record_key,),
                        )
                else:
                    self.case.path.write_bytes(
                        self.case.path.read_bytes().replace(
                            b"Use Python.", b"Use Erlang."
                        )
                    )
                with self.assertRaisesRegex(ValueError, "evidence_changed"):
                    self.check(records, ReadBudget(17_000))

    def test_reuse_key_includes_physical_record_length(self) -> None:
        records = self.selections()
        original = records[1].original_locator
        assert not isinstance(original, CursorOriginal)
        changed = original.model_copy(
            update={"byte_length": original.byte_length - 1}
        )
        self.case.conn.execute(
            "UPDATE review_evidence SET original_locator_json=? WHERE id=?",
            (changed.model_dump_json(), records[1].id),
        )
        with self.assertRaisesRegex(ValueError, "original_unavailable"):
            self.check(records, ReadBudget(40_000))

    def test_distinct_locators_still_consume_distinct_reads(self) -> None:
        records = self.selections()
        other = self.case.select(5, "/payload/content/0/text")
        self.case.persist(other)
        size = len(line(self.case.records[1]))
        budget = ReadBudget(size)
        with self.assertRaisesRegex(ValueError, "evidence_limit"):
            self.check((records[0], other), budget)
        self.assertEqual(budget.bytes_left, 0)

    def test_repeated_selections_cannot_accumulate_unbounded_excerpts(
        self,
    ) -> None:
        self.case.records[1] = user_msg(1, "Use " + "Python " * 200)
        self.case.path = self.case.write(rollout(), self.case.records)
        self.case.run_ingest(full=True)
        records = tuple(
            review_evidence.select_evidence(
                self.case.home,
                self.case.locator(2),
                EvidenceSelector(
                    json_pointer="/payload/content/0/text", start=0, end=end
                ),
            )
            for end in range(1400, 1404)
        )
        record = records[0]
        for item in records:
            self.case.persist(item)
        policy = self.case.policy(record).model_copy(
            update={"max_input_bytes": 4096}
        )
        with self.assertRaises(ValueError) as caught:
            review_evidence.validate_evidence(
                self.case.conn,
                self.case.home,
                tuple(r.id for r in records),
                record.scope,
                policy,
            )
        self.assertEqual(
            getattr(caught.exception, "code", None), "evidence_selection_limit"
        )
