"""Regress native startup refresh and missed-boundary reporting composition."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from typing import Any
from unittest import mock

from muninn import cli_hook, cursor_import, hook_learn
from tests.knowledge_context_support import KnowledgeContextCase


class HookLearningLifecycleTests(KnowledgeContextCase):
    """Use real originals, knowledge producers and SQLite contention."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = self.policy.model_copy(update={"context_bytes": 10000})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (
                json.dumps(self.policy.model_dump(mode="json")),
                self.digest(self.policy),
            ),
        )
        self.env = dict(os.environ) | {
            "HOME": str(self.tmp / "native-home"),
            "MUNINN_HOME": str(self.home),
        }
        self.database = self.tmp / "cursor.vscdb"
        self.env["MUNINN_CURSOR_DB"] = str(self.database)
        with contextlib.closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TABLE cursorDiskKV(key TEXT,value TEXT)")
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                (
                    "composerData:active",
                    '{"composerId":"active",'
                    '"fullConversationHeadersOnly":[]}',
                ),
            )

    def cursor_payload(self, event: str) -> dict[str, Any]:
        """Build the documented native event for one empty composer."""
        data = {
            "hook_event_name": event,
            "conversation_id": "active",
            "generation_id": "generation",
            "model": "explicit",
            "cursor_version": "snapshot",
            "workspace_roots": [self.scope.project_id],
        }
        if event == "sessionStart":
            data.update(session_id="active", is_background_agent=False)
        else:
            data.update(
                trigger="auto",
                context_usage_percent=85,
                context_tokens=120000,
                context_window_size=128000,
                message_count=1,
                messages_to_compact=1,
                is_first_compaction=True,
            )
        return data

    def dispatch(
        self, event: str, data: dict[str, Any], provider: str
    ) -> dict[str, Any]:
        """Run the real compact CLI with temporary provider and data homes."""
        output = io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(data).encode()))
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(cli_hook.sys, "stdin", stream),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                cli_hook.hook_main([event, "--provider", provider], self.env),
                0,
            )
        raw = output.getvalue()
        self.assertEqual(len(raw.splitlines()), 1)
        return json.loads(raw)

    def logs(self) -> list[dict[str, Any]]:
        """Read only the isolated call log's content-free observations."""
        return [
            json.loads(line)
            for line in (self.home / "calls.jsonl").read_text().splitlines()
        ]

    def test_empty_cursor_start_refreshes_permitted_context_without_capture(
        self,
    ) -> None:
        ident = self.add()
        counts = cursor_import.run(
            self.home,
            self.database,
            str(self.scope.project_id),
            0,
            conversation_id="active",
        )
        self.assertEqual(counts["conversations_seen"], 0)
        with mock.patch.object(hook_learn, "capture_boundary") as captured:
            answer = self.dispatch(
                "session-start", self.cursor_payload("sessionStart"), "cursor"
            )
            captured.assert_not_called()
        self.assertEqual(set(answer), {"additional_context"})
        self.assertIn(ident, answer["additional_context"])
        self.assertIn("Use Python.", answer["additional_context"])
        self.assertNotIn("cursor_import_failed", json.dumps(self.logs()))
        self.assertFalse(any(row.get("accepted") for row in self.logs()))
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM source WHERE provider='cursor'"
            ).fetchone()[0],
            0,
        )

    def test_real_exclusive_policy_preload_busy_is_observed(self) -> None:
        data = {
            "hook_event_name": "SessionEnd",
            "session_id": "native",
            "cwd": str(self.tmp),
            "transcript_path": str(self.path),
            "reason": "other",
        }
        self.conn.execute("BEGIN EXCLUSIVE")
        try:
            self.assertEqual(self.dispatch("session-end", data, "codex"), {})
        finally:
            self.conn.rollback()
        captures = [
            row
            for row in self.logs()
            if row.get("event") == "review_enqueue_busy"
        ]
        self.assertEqual(len(captures), 1)
        self.assertEqual(
            (
                captures[0]["accepted"],
                captures[0]["missed_acknowledgments"],
                captures[0]["recovery_gaps"],
            ),
            (False, 1, 1),
        )
        self.assertEqual(self.logs()[-1]["error"], "review_enqueue_busy")
        self.assertNotIn("database is locked", json.dumps(self.logs()))

    def test_empty_precompact_records_gap_without_capture_or_acceptance(
        self,
    ) -> None:
        with mock.patch.object(hook_learn, "capture_boundary") as captured:
            answer = self.dispatch(
                "pre-compact", self.cursor_payload("preCompact"), "cursor"
            )
            captured.assert_not_called()
        self.assertEqual(answer, {})
        gaps = [
            row
            for row in self.logs()
            if row.get("missed_acknowledgments") == 1
        ]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(
            (gaps[0]["accepted"], gaps[0]["recovery_gaps"]), (False, 1)
        )
        self.assertEqual(self.logs()[-1]["counts"]["conversations_seen"], 0)

    def test_cursor_start_preserves_workspace_and_disable_controls(
        self,
    ) -> None:
        for roots in (
            [],
            ["relative"],
            [str(self.tmp), str(self.tmp / "other")],
        ):
            with self.subTest(roots=roots):
                data = self.cursor_payload("sessionStart")
                data["workspace_roots"] = roots
                self.assertEqual(
                    self.dispatch("session-start", data, "cursor"), {}
                )
        self.env["MUNINN_HOOK_DISABLE"] = "1"
        self.assertEqual(
            self.dispatch(
                "session-start", self.cursor_payload("sessionStart"), "cursor"
            ),
            {},
        )
