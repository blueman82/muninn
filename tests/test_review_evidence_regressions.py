"""Guard erased ancestry and Cursor's existing imported coordinates."""

from __future__ import annotations

import hashlib
import sqlite3
import unittest
from contextlib import closing

from muninn import cursor_import, review_evidence, tombstones
from muninn.review_evidence_models import (
    CursorOriginal,
    EvidenceLocator,
    EvidenceSelector,
    EvidenceWindow,
    SourceRef,
)
from tests import test_review_evidence as evidence_tests


class ReviewEvidenceRegressionTests(unittest.TestCase):
    """Reuse disposable fixtures without inheriting their test cases."""

    def setUp(self) -> None:
        self.fixture = evidence_tests.ReviewEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_missing_ancestor_tombstone_blocks_selection_and_validation(
        self,
    ) -> None:
        case = self.fixture
        record = case.select(2, "/payload/content/0/text")
        case.persist(record)
        case.conn.execute(
            "UPDATE source SET forked_from_id='missing-parent'"
            " WHERE thread_id=?",
            (record.thread_id,),
        )
        case.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',"
            "'missing-parent',0,?,0)",
            ("ev2:" + "a" * 64,),
        )
        self.assertEqual(
            tombstones.erased_events(
                case.conn, "codex", record.thread_id, "missing-parent"
            ),
            {"ev2:" + "a" * 64},
        )
        for validate in (False, True):
            with (
                self.subTest(validate=validate),
                self.assertRaises(ValueError),
            ):
                if validate:
                    case.validate(record)
                else:
                    case.select(2, "/payload/content/0/text")

    def test_unretrievable_ancestor_provenance_fails_closed(self) -> None:
        case = self.fixture
        record = case.select(2, "/payload/content/0/text")
        case.persist(record)
        case.conn.execute(
            "UPDATE source SET forked_from_id='unretrievable-parent'"
            " WHERE thread_id=?",
            (record.thread_id,),
        )
        for validate in (False, True):
            with (
                self.subTest(validate=validate),
                self.assertRaises(ValueError),
            ):
                if validate:
                    case.validate(record)
                else:
                    case.select(2, "/payload/content/0/text")

    def test_cursor_whitespace_preserves_later_coordinates_and_window(
        self,
    ) -> None:
        case = self.fixture
        path = case.tmp / "cursor-regression.sqlite"
        blank = b'{"type":1,"text":"   "}'
        visible = b'{"type":1,"text":"Please keep Python."}'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE cursorDiskKV(key TEXT,value BLOB)")
            db.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                [
                    (
                        "composerData:chat",
                        b'{"fullConversationHeadersOnly":'
                        b'[{"bubbleId":"blank"},{"bubbleId":"visible"}]}',
                    ),
                    ("bubbleId:chat:blank", blank),
                    ("bubbleId:chat:visible", visible),
                ],
            )
        cursor_import.run(case.home, path, str(case.tmp), 0.0)
        before = path.read_bytes()
        coordinates = case.conn.execute(
            "SELECT line,part FROM event WHERE source_id="
            "(SELECT id FROM source WHERE provider='cursor') ORDER BY line"
        ).fetchall()
        self.assertEqual([tuple(row) for row in coordinates], [(1, 1), (2, 1)])
        source = SourceRef(provider="cursor", thread_id="chat")
        digest = hashlib.sha256(visible).hexdigest()
        locator = EvidenceLocator(
            source=source, line=2, part=1, line_sha256=digest
        )
        selector = EvidenceSelector(json_pointer="/text", start=None, end=None)
        record = review_evidence.select_evidence(case.home, locator, selector)
        self.assertEqual(
            (record.line, record.excerpt), (2, "Please keep Python.")
        )
        original = record.original_locator
        assert isinstance(original, CursorOriginal)
        self.assertEqual(original.record_key, "bubbleId:chat:visible")
        case.persist(record)
        self.assertEqual(case.validate(record), (record,))
        window = EvidenceWindow(
            source=source, from_line=1, through_line=2, boundary_hash=digest
        )
        initial = review_evidence.initial_evidence(case.home, window, 100_000)
        self.assertEqual([e.line for e in initial.excerpts], [2])
        with self.assertRaises(ValueError):
            review_evidence.select_evidence(
                case.home,
                locator.model_copy(
                    update={
                        "line": 1,
                        "line_sha256": hashlib.sha256(blank).hexdigest(),
                    }
                ),
                selector,
            )
        self.assertEqual(path.read_bytes(), before)
