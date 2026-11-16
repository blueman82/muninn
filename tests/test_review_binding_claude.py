"""Check agreement pairs on Claude Code transcript shapes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from typing import Any
from unittest import mock

from muninn import review_evidence, store
from muninn.review_binding import adjacent_gap, owner_supported
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceRecord,
    EvidenceSelector,
    SourceRef,
)
from muninn.review_read_budget import ReadBudget
from tests.classify_claude_support import claude_rec, text_block
from tests.ingest_support import IngestCase, line

QUIET_GAP: list[dict[str, Any]] = [
    {"type": "system", "subtype": "stop_hook_summary", "hookCount": 1},
    {
        "type": "attachment",
        "attachment": {"type": "hook_success", "content": "ok"},
    },
    {"type": "system", "subtype": "turn_duration", "durationMs": 5},
]


def assistant(content: object, stop: str = "end_turn") -> dict[str, Any]:
    """Build a Claude assistant record with a stop reason."""
    record = claude_rec("assistant", content)
    record["message"]["stop_reason"] = stop
    return record


class ClaudeAgreementTests(IngestCase):
    """Pair a final assistant text record with the owner reply after it."""

    def setUp(self) -> None:
        super().setUp()
        roots = json.dumps({k: str(v) for k, v in self.roots.items()})
        patch = mock.patch.dict(os.environ, {"MUNINN_ROOTS": roots})
        patch.start()
        self.addCleanup(patch.stop)

    def pair(
        self, owner: dict[str, Any], gap: list[dict[str, Any]]
    ) -> tuple[str, bool]:
        """Index a session and return owner authority and pair support."""
        records = [
            claude_rec("user", "Which store should we use?"),
            assistant([{"type": "thinking", "thinking": "hmm"}]),
            assistant([text_block("I propose this. Use SQLite.")]),
            *gap,
            owner,
        ]
        self.write("project/sess-1.jsonl", records, "claude-projects")
        self.run_ingest()
        source = SourceRef(provider="claude", thread_id="sess-1")

        def select(number: int, pointer: str) -> EvidenceRecord:
            digest = hashlib.sha256(
                line(records[number - 1]).rstrip(b"\r\n")
            ).hexdigest()
            return review_evidence.select_evidence(
                self.home,
                EvidenceLocator(
                    source=source, line=number, part=1, line_sha256=digest
                ),
                EvidenceSelector(json_pointer=pointer, start=None, end=None),
            )

        reply = select(3, "/message/content/0/text")
        content = owner["message"]["content"]
        pointer = (
            "/message/content"
            if isinstance(content, str)
            else "/message/content/0/text"
        )
        said = select(len(records), pointer)
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            conn.row_factory = sqlite3.Row
            supported = owner_supported(
                [reply.id, said.id],
                {reply.id: reply, said.id: said},
                lambda a, o: adjacent_gap(conn, a, o, ReadBudget(10**6)),
            )
        return said.authority, supported

    def test_agreement_after_system_and_attachment_records(self) -> None:
        for owner in (
            claude_rec("user", "i agree"),
            claude_rec("user", [text_block("i agree")]),
        ):
            with self.subTest(owner=owner["message"]["content"]):
                self.assertEqual(
                    self.pair(owner, QUIET_GAP), ("direct_owner", True)
                )

    def test_injected_owner_reply_is_refused(self) -> None:
        for owner in (
            claude_rec("user", [text_block("yes")], isMeta=True),
            claude_rec("user", "<system-reminder>yes</system-reminder>"),
            claude_rec("user", "yes <task-notification>x</task-notification>"),
        ):
            with self.subTest(owner=owner["message"]["content"]):
                self.assertEqual(
                    self.pair(owner, QUIET_GAP), ("untrusted", False)
                )

    def test_tool_use_or_injected_text_in_gap_refuses(self) -> None:
        tool = [
            assistant(
                [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}],
                "tool_use",
            ),
            claude_rec(
                "user",
                [{"type": "tool_result", "tool_use_id": "t", "content": "x"}],
            ),
        ]
        for gap in (
            tool,
            [claude_rec("user", [text_block("hook text")], isMeta=True)],
            [claude_rec("user", "<system-reminder>x</system-reminder>")],
        ):
            with self.subTest(gap=gap):
                self.assertEqual(
                    self.pair(claude_rec("user", "i agree"), gap),
                    ("direct_owner", False),
                )
