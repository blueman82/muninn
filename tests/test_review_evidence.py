"""Exercise permitted originals and stable evidence under disposable homes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from unittest import mock

from muninn import cursor_import, review_evidence
from muninn.knowledge_models import Scope
from muninn.review_evidence_models import (
    CursorOriginal,
    EvidenceLocator,
    EvidenceRecord,
    EvidenceSelector,
    EvidenceWindow,
    JsonlOriginal,
    SourceRef,
)
from muninn.review_models import ReviewPolicy
from tests.classify_claude_support import (
    claude_rec,
    text_block,
    tool_result,
    tool_use,
)
from tests.ingest_support import TID, IngestCase, fc_output, line, rollout
from tests.test_classify import codex_meta, function_call, reply, user_msg
from tests.test_review_shared_models import policy_data


class ReviewEvidenceTests(IngestCase):
    """Read actual synthetic originals, never a mocked evidence reader."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(
            os.environ,
            {
                "MUNINN_ROOTS": json.dumps(
                    {k: str(v) for k, v in self.roots.items()}
                )
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.records = [
            codex_meta("user", TID),
            user_msg(1, "Use Python for this project."),
            function_call(2, "exec_command", '{"cmd":"test"}', "call-1"),
            fc_output(
                3, "call-1", "Process exited with code 0\nOutput: passed"
            ),
            reply(4, "The checks passed."),
        ]
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        self.source_ref = SourceRef(provider="codex", thread_id=TID)

    def locator(self, number: int) -> EvidenceLocator:
        """Build a stable reference from independently serialized input."""
        return EvidenceLocator(
            source=self.source_ref,
            line=number,
            part=1,
            line_sha256=hashlib.sha256(
                line(self.records[number - 1]).rstrip(b"\r\n")
            ).hexdigest(),
        )

    def select(self, number: int, pointer: str) -> EvidenceRecord:
        """Select an exact permitted field from the fixture."""
        return review_evidence.select_evidence(
            self.home,
            self.locator(number),
            EvidenceSelector(json_pointer=pointer, start=None, end=None),
        )

    def persist(self, record: EvidenceRecord) -> None:
        """Persist one returned record using the accepted SQL column shape."""
        data = record.model_dump(mode="json")
        for field in ("original_locator", "selector", "scope"):
            data[f"{field}_json"] = json.dumps(data.pop(field))
        columns = ",".join(data)
        marks = ",".join("?" for _ in data)
        self.conn.execute(
            f"INSERT INTO review_evidence({columns}) VALUES({marks})",
            tuple(data.values()),
        )

    def policy(self, record: EvidenceRecord) -> ReviewPolicy:
        """Supply explicit projection and validation limits."""
        data = policy_data()
        data.update(
            projection_version=record.projection_version,
            max_input_bytes=100_000,
            context_bytes=100_000,
        )
        return ReviewPolicy.model_validate(data)

    def validate(self, record: EvidenceRecord) -> tuple[EvidenceRecord, ...]:
        """Validate persisted evidence with its explicit test policy."""
        return review_evidence.validate_evidence(
            self.conn,
            self.home,
            (record.id,),
            record.scope,
            self.policy(record),
        )

    def test_selected_tool_result_keeps_original_coordinates(self) -> None:
        record = self.select(4, "/payload/output")
        self.assertEqual(
            (record.provider, record.thread_id, record.line, record.part),
            ("codex", TID, 4, 1),
        )
        self.assertEqual(record.line_sha256, self.locator(4).line_sha256)
        self.assertEqual(
            record.excerpt, "Process exited with code 0\nOutput: passed"
        )
        self.assertEqual(record.evidence_type, "tool_result")
        self.assertEqual(record.authority, "untrusted")
        self.assertIsInstance(record.original_locator, JsonlOriginal)
        original = record.original_locator
        assert isinstance(original, JsonlOriginal)
        self.assertEqual(original.path, self.path)
        self.assertEqual(
            original.byte_offset, sum(len(line(r)) for r in self.records[:3])
        )
        self.assertEqual(original.byte_length, len(line(self.records[3])))
        self.assertTrue(record.source_version)
        self.assertTrue(record.projection_version)
        self.assertEqual(record.scope.task, None)
        self.assertEqual(len(record.selector_digest), 64)

    def test_changed_hash_and_missing_original_refused(self) -> None:
        record = self.select(4, "/payload/output")
        self.persist(record)
        policy = self.policy(record)
        with self.conn:
            checked = review_evidence.validate_evidence(
                self.conn, self.home, (record.id,), record.scope, policy
            )
        self.assertEqual(checked, (record,))
        self.path.write_bytes(
            self.path.read_bytes().replace(b"passed", b"failed")
        )
        with self.assertRaises(ValueError):
            self.select(4, "/payload/output")
        with self.assertRaises(ValueError):
            review_evidence.validate_evidence(
                self.conn, self.home, (record.id,), record.scope, policy
            )
        self.path.unlink()
        with self.assertRaises(ValueError):
            review_evidence.validate_evidence(
                self.conn, self.home, (record.id,), record.scope, policy
            )

    def test_analysis_and_unknown_fields_excluded(self) -> None:
        self.records[4]["payload"].update(channel="analysis", private="hidden")
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        for pointer in (
            "/payload/content/0/text",
            "/payload/private",
            "/payload",
        ):
            with self.subTest(pointer=pointer), self.assertRaises(ValueError):
                self.select(5, pointer)
        window = EvidenceWindow(
            source=self.source_ref,
            from_line=2,
            through_line=5,
            boundary_hash=self.locator(5).line_sha256,
        )
        initial = review_evidence.initial_evidence(self.home, window, 100_000)
        self.assertEqual([e.line for e in initial.excerpts], [2])
        self.assertEqual([e.locator.line for e in initial.outcomes], [3, 4])
        self.assertNotIn("hidden", initial.model_dump_json())
        self.assertNotIn("Output: passed", initial.model_dump_json())

    def test_quoted_user_text_is_not_direct_owner(self) -> None:
        text = (
            '🙂 A colleague wrote: "Use Rust everywhere." '
            "I am quoting, not directing."
        )
        self.records[1] = user_msg(1, text)
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        start = text.index("Use Rust")
        record = review_evidence.select_evidence(
            self.home,
            self.locator(2),
            EvidenceSelector(
                json_pointer="/payload/content/0/text",
                start=start,
                end=start + 20,
            ),
        )
        self.assertEqual(record.excerpt, "Use Rust everywhere.")
        self.assertNotEqual(record.authority, "direct_owner")
        self.assertEqual(record.selector.start, start)
        self.assertEqual(record.evidence_type, "owner_statement")

    def test_tool_request_does_not_prove_success(self) -> None:
        request = self.select(3, "/payload/arguments")
        report = self.select(5, "/payload/content/0/text")
        self.assertEqual(
            (request.evidence_type, request.authority),
            ("tool_request", "untrusted"),
        )
        self.assertEqual(
            (report.evidence_type, report.authority),
            ("assistant_report", "reported"),
        )
        self.assertNotEqual(request.evidence_type, "observed_outcome")

    def test_reingest_event_ids_do_not_change_support(self) -> None:
        record = self.select(2, "/payload/content/0/text")
        self.persist(record)
        self.conn.execute("DELETE FROM event")
        self.run_ingest(full=True)
        after = self.select(2, "/payload/content/0/text")
        for attribute in ("line", "part", "line_sha256", "selector_digest"):
            self.assertEqual(
                getattr(record, attribute), getattr(after, attribute)
            )

        self.assertEqual(self.validate(record), (record,))

    def test_validation_rejects_scope_state_projection_and_limits(
        self,
    ) -> None:
        record = self.select(2, "/payload/content/0/text")
        self.persist(record)
        policy = self.policy(record)
        for scope, changed in (
            (Scope(project_id="elsewhere", task=None), policy),
            (
                record.scope,
                policy.model_copy(update={"projection_version": "unknown"}),
            ),
            (record.scope, policy.model_copy(update={"max_input_bytes": 1})),
        ):
            with (
                self.subTest(scope=scope, policy=changed),
                self.assertRaises(ValueError),
            ):
                review_evidence.validate_evidence(
                    self.conn, self.home, (record.id,), scope, changed
                )
        self.conn.execute(
            "UPDATE review_evidence SET state='erased',excerpt=NULL"
        )
        with self.assertRaises(ValueError):
            review_evidence.validate_evidence(
                self.conn, self.home, (record.id,), record.scope, policy
            )

    def test_initial_budget_and_boundary_are_enforced(self) -> None:
        window = EvidenceWindow(
            source=self.source_ref,
            from_line=2,
            through_line=5,
            boundary_hash=self.locator(5).line_sha256,
        )
        initial = review_evidence.initial_evidence(self.home, window, 1)
        self.assertLessEqual(initial.bytes_used, 1)
        self.assertTrue(initial.truncated)
        self.assertEqual(initial.excerpts, ())
        with self.assertRaises(ValueError):
            review_evidence.initial_evidence(
                self.home,
                window.model_copy(update={"boundary_hash": "0" * 64}),
                100_000,
            )

    def test_claude_original_block_positions_survive_filtering(self) -> None:
        records = [
            claude_rec("user", "Use the project test command."),
            claude_rec(
                "assistant",
                [
                    text_block("Trying now"),
                    tool_use("Bash", {"command": "test"}, "a"),
                    tool_use("Bash", {"command": "test"}, "b"),
                ],
            ),
            claude_rec(
                "user",
                [
                    tool_result("a", "ok"),
                    text_block("Use Rust everywhere."),
                    {"type": "thinking", "thinking": "hidden"},
                    tool_result("b", "failed", True),
                ],
            ),
        ]
        self.write("project/sess-1.jsonl", records, "claude-projects")
        self.run_ingest()
        source = SourceRef(provider="claude", thread_id="sess-1")
        digest = hashlib.sha256(line(records[2]).rstrip(b"\r\n")).hexdigest()
        window = EvidenceWindow(
            source=source, from_line=3, through_line=3, boundary_hash=digest
        )
        initial = review_evidence.initial_evidence(self.home, window, 100_000)
        self.assertEqual([o.locator.part for o in initial.outcomes], [1, 4])
        self.assertEqual(initial.excerpts[0].authority, "untrusted")
        locator = EvidenceLocator(
            source=source, line=3, part=4, line_sha256=digest
        )
        selector = EvidenceSelector(
            json_pointer="/message/content/3/content", start=None, end=None
        )
        selected = review_evidence.select_evidence(
            self.home, locator, selector
        )
        self.assertEqual(selected.excerpt, "failed")
        self.assertNotIn("hidden", initial.model_dump_json())
        with self.assertRaises(ValueError):
            review_evidence.select_evidence(
                self.home, locator.model_copy(update={"part": 1}), selector
            )
        self.persist(selected)
        self.run_ingest(full=True)
        self.assertEqual(
            self.validate(selected),
            (selected,),
        )

    def test_cursor_exact_original_cell_is_read_only_and_revalidated(
        self,
    ) -> None:
        path = self.tmp / "cursor.sqlite"
        raw = b'{"type":1,"text":"Please keep Python.","thinking":"hidden"}'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE cursorDiskKV(key TEXT,value BLOB)")
            db.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                [
                    (
                        "composerData:chat",
                        b'{"fullConversationHeadersOnly":[{"bubbleId":"a"}]}',
                    ),
                    ("bubbleId:chat:a", raw),
                ],
            )
        cursor_import.run(self.home, path, str(self.tmp), 0.0)
        before = path.read_bytes()
        locator = EvidenceLocator(
            source=SourceRef(provider="cursor", thread_id="chat"),
            line=1,
            part=1,
            line_sha256=hashlib.sha256(raw).hexdigest(),
        )
        selector = EvidenceSelector(json_pointer="/text", start=None, end=None)
        record = review_evidence.select_evidence(self.home, locator, selector)
        self.assertEqual(record.excerpt, "Please keep Python.")
        self.assertIsInstance(record.original_locator, CursorOriginal)
        self.persist(record)
        self.assertEqual(
            self.validate(record),
            (record,),
        )
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "UPDATE cursorDiskKV SET value=? WHERE key='bubbleId:chat:a'",
                (b'{"type":1,"text":"changed"}',),
            )
        with self.assertRaises(ValueError):
            self.validate(record)

    def test_tombstone_and_partial_secret_span_fail_closed(self) -> None:
        record = self.select(2, "/payload/content/0/text")
        self.persist(record)
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',?,2,?,0)",
            (TID, record.line_sha256),
        )
        with self.assertRaises(ValueError):
            self.validate(record)
        self.records[1] = user_msg(
            1, "Please use token=abcdefghijklmno safely."
        )
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        selector = EvidenceSelector(
            json_pointer="/payload/content/0/text", start=20, end=25
        )
        with self.assertRaises(ValueError):
            review_evidence.select_evidence(
                self.home, self.locator(2), selector
            )

    def test_claude_intermediate_reply_not_initial_input(self) -> None:
        record = claude_rec(
            "assistant",
            [
                text_block("intermediate"),
                tool_use("Bash", {"command": "test"}),
            ],
        )
        record["message"]["stop_reason"] = "tool_use"
        self.write("project/sess-1.jsonl", [record], "claude-projects")
        self.run_ingest()
        window = EvidenceWindow(
            source=SourceRef(provider="claude", thread_id="sess-1"),
            from_line=1,
            through_line=1,
            boundary_hash=hashlib.sha256(
                line(record).rstrip(b"\r\n")
            ).hexdigest(),
        )
        initial = review_evidence.initial_evidence(self.home, window, 100_000)
        self.assertEqual(initial.excerpts, ())
        self.assertEqual([o.locator.part for o in initial.outcomes], [2])
