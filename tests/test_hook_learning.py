"""Exercise native lifecycle capture against disposable indexed originals."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from typing import Any
from unittest import mock

from muninn import cli_hook, cursor_import, hook_learn, review_requests, store
from tests.ingest_support import SEG, TID, line, rollout
from tests.test_classify import codex_meta, user_msg
from tests.test_review_requests import RequestCase


class HookLearningTests(RequestCase):
    """Require durable, exact-source review requests from lifecycle hooks."""

    def setUp(self) -> None:
        super().setUp()
        self.records[0]["payload"]["session_id"] = TID
        self.path.write_bytes(b"".join(map(line, self.records)))
        self.bump(self.path)
        self.run_ingest()
        self.env = dict(os.environ) | {
            "HOME": str(self.tmp / "native-home"),
            "MUNINN_HOME": str(self.home),
        }

    def payload(
        self, event: str = "SessionEnd", **changes: Any
    ) -> dict[str, Any]:
        """Build a complete pinned Codex native payload for one exact path."""
        data = {
            "session_id": TID,
            "cwd": str(self.tmp),
            "transcript_path": str(self.path),
            "hook_event_name": event,
        }
        if event == "SessionEnd":
            data["reason"] = "other"
        elif event == "SessionStart":
            data.update(
                source="startup", model="explicit", permission_mode="default"
            )
        else:
            data.update(turn_id="turn", model="explicit", trigger="auto")
        return data | changes

    def call_hook(
        self, event: str, payload: dict[str, Any], provider: str = "codex"
    ) -> dict[str, Any]:
        """Run the real compact provider entry point with isolated stdin."""
        output = io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
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

    def test_provider_event_names_and_payloads(self) -> None:
        self.assertEqual(self.call_hook("session-end", self.payload()), {})
        rows = self.conn.execute(
            "SELECT trigger,provider,thread_id,from_line,through_line "
            "FROM review_request ORDER BY from_line"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("hook", "codex", TID, 1, 2), ("hook", "codex", TID, 3, 3)],
        )

    def count(self, table: str, provider: str = "codex") -> int:
        """Read durable source or request counts from the real store."""
        return self.conn.execute(
            f"SELECT count(*) FROM {table} WHERE provider=?", (provider,)
        ).fetchone()[0]

    def set_policy(self, size: int) -> None:
        """Install an explicit byte grant without introducing a default."""
        self.policy = self.policy.model_copy(update={"max_input_bytes": size})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (
                json.dumps(self.policy.model_dump(mode="json")),
                self.digest(self.policy),
            ),
        )

    def capture(self, **changes: Any) -> hook_learn.HookCapture:
        """Run the public internal boundary with the actual isolated policy."""
        with mock.patch.dict(os.environ, self.env, clear=True):
            return hook_learn.capture_boundary(
                self.home,
                "codex",
                "session-end",
                json.dumps(self.payload(**changes)).encode(),
                self.policy,
            )

    def test_exact_path_hash_and_missing_original_refuse(self) -> None:
        for path in (str(self.path) + ".missing", "relative", None):
            with self.subTest(path=path):
                self.assertFalse(self.capture(transcript_path=path).accepted)
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b'"user"', b'"other"', 1))
        self.assertEqual(self.capture().code, "evidence_changed")
        self.path.write_bytes(original)
        alias = self.path.with_name("alias.jsonl")
        alias.symlink_to(self.path)
        self.assertFalse(self.capture(transcript_path=str(alias)).accepted)
        self.assertEqual(self.count("review_request"), 0)

    def test_explicit_metadata_session_required_without_id_alias(self) -> None:
        for identity in (None, "different"):
            self.records[0]["payload"]["session_id"] = identity
            self.path.write_bytes(b"".join(map(line, self.records)))
            self.bump(self.path)
            self.run_ingest()
            self.assertEqual(self.capture().code, "source_unavailable")

    def test_reviewer_session_cannot_enqueue_itself(self) -> None:
        for kind, extra in (
            ("guardian_review", {}),
            ("subagent", {}),
            ("user", {"source": {"subagent": {"other": "worker"}}}),
            ("user", {"parent_thread_id": "parent"}),
        ):
            self.records[0] = codex_meta(kind, TID, session_id=TID)
            self.records[0]["payload"].update(extra)
            self.path.write_bytes(b"".join(map(line, self.records)))
            self.bump(self.path)
            self.run_ingest()
            self.assertFalse(self.capture().accepted)
        self.records[0] = codex_meta("user", TID, session_id=TID)
        self.path.write_bytes(b"".join(map(line, self.records)))
        self.bump(self.path)
        self.run_ingest()
        self.assertFalse(self.capture(agent_id=TID).accepted)
        self.records[0] = codex_meta("subagent", SEG, session_id=TID)
        child = self.write(rollout(SEG), self.records)
        self.run_ingest()
        self.assertFalse(self.capture(transcript_path=str(child)).accepted)

    def test_continuation_uses_indexed_path_identity(self) -> None:
        path = self.write(rollout(SEG), self.records)
        self.run_ingest()
        result = self.capture(transcript_path=str(path))
        self.assertTrue(result.accepted, result.code)
        self.assertEqual(
            {
                row[0]
                for row in self.conn.execute(
                    "SELECT thread_id FROM review_request"
                )
            },
            {SEG},
        )

    def test_precompact_and_invalid_native_event_names(self) -> None:
        start = self.call_hook("session-start", self.payload("SessionStart"))
        self.assertIn("hookSpecificOutput", start)
        self.call_hook("pre-compact", self.payload("PreCompact"))
        self.assertEqual(self.count("review_request"), 2)
        self.assertEqual(
            self.capture(hook_event_name="Stop").code, "rejected_validation"
        )
        self.assertEqual(
            self.capture(reason="invented").code, "rejected_validation"
        )
        self.assertEqual(
            self.call_hook(
                "session-start", self.payload("SessionStart", source="fork")
            ),
            {},
        )

    def test_lag_and_partial_tail_only_capture_indexed_prefix(self) -> None:
        self.set_policy(65536)
        self.append(
            self.path, [user_msg(4, "not indexed yet")], b'{"partial":'
        )
        self.assertTrue(self.capture().accepted)
        self.assertEqual(
            self.conn.execute(
                "SELECT max(through_line) FROM review_request"
            ).fetchone()[0],
            3,
        )
        self.run_ingest()
        self.assertTrue(self.capture().accepted)
        self.assertEqual(
            self.conn.execute(
                "SELECT max(through_line) FROM review_request"
            ).fetchone()[0],
            4,
        )
        self.assertFalse(self.capture().accepted)

    def test_stale_policy_and_missing_policy_refuse(self) -> None:
        changed = self.policy.model_copy(update={"policy_id": "changed"})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (
                json.dumps(changed.model_dump(mode="json")),
                self.digest(changed),
            ),
        )
        self.assertEqual(self.capture().code, "review_policy_required")
        self.conn.execute("UPDATE review_control SET admission=0")
        self.assertEqual(self.capture().code, "review_policy_required")

    def test_one_nonrefundable_original_budget_rolls_back_all_windows(
        self,
    ) -> None:
        self.set_policy(len(self.path.read_bytes()) + 10)
        self.assertEqual(self.capture().code, "evidence_limit")
        self.assertEqual(self.count("review_request"), 0)
        self.assertEqual(self.count("review_source"), 0)

    def test_cancelled_and_erased_windows_do_not_revive(self) -> None:
        result = self.capture()
        self.assertTrue(result.accepted)
        for ident in result.request_ids:
            self.conn.execute("BEGIN IMMEDIATE")
            review_requests.cancel_request(self.conn, ident, 12.0)
            self.conn.commit()
        self.conn.execute(
            "UPDATE review_source SET examined_line=0,eligible_line=0,"
            "boundary_hash=NULL"
        )
        self.assertFalse(self.capture().accepted)
        self.records.append(user_msg(4, "erased record"))
        self.append(self.path, [self.records[-1]])
        self.run_ingest()
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',?,4,?,13.0)",
            (TID, self.window(4, 4).boundary_hash),
        )
        self.assertEqual(self.capture().code, "evidence_erased")

    def test_busy_capture_and_committed_acknowledgment(self) -> None:
        with store.writer_lock(self.home, wait_s=0):
            result = self.capture()
        self.assertEqual(
            (result.accepted, result.request_ids, result.code),
            (False, (), "review_enqueue_busy"),
        )
        self.conn.execute("BEGIN IMMEDIATE")
        busy = self.capture()
        self.conn.rollback()
        self.assertEqual(busy.code, "review_enqueue_busy")
        result = self.capture()
        with (
            store.writer_lock(self.home, wait_s=0),
            contextlib.closing(
                store.connect_ro(store.db_path(self.home))
            ) as reader,
        ):
            actual = tuple(
                row[0]
                for row in reader.execute(
                    "SELECT id FROM review_request ORDER BY from_line"
                )
            )
        self.assertTrue(result.accepted)
        self.assertEqual(result.request_ids, actual)

    def test_commit_failure_never_acknowledges_or_leaves_partial_rows(
        self,
    ) -> None:
        failing = sqlite3.connect(
            store.db_path(self.home),
            isolation_level=None,
            factory=FailedCommit,
        )
        failing.row_factory = sqlite3.Row
        with mock.patch.object(
            hook_learn.store, "connect_rw", return_value=failing
        ):
            result = self.capture()
        self.assertEqual((result.accepted, result.request_ids), (False, ()))
        self.assertEqual(self.count("review_request"), 0)

    def test_capture_never_launches_process_or_ingests(self) -> None:
        with (
            mock.patch("subprocess.Popen", side_effect=RuntimeError),
            mock.patch("muninn.ingest.run_pass", side_effect=RuntimeError),
        ):
            self.assertTrue(self.capture().accepted)

    def test_claude_explicit_session_metadata_and_precompact(self) -> None:
        record = {
            "type": "user",
            "sessionId": "claude-session",
            "cwd": str(self.tmp),
            "message": {"role": "user", "content": "owner statement"},
        }
        path = self.write(
            "project/claude-session.jsonl", [record], "claude-projects"
        )
        self.run_ingest()
        payload = {
            "session_id": "claude-session",
            "cwd": str(self.tmp),
            "transcript_path": str(path),
            "hook_event_name": "PreCompact",
            "trigger": "auto",
            "custom_instructions": None,
        }
        self.call_hook("pre-compact", payload, "claude")
        self.assertEqual(self.count("review_request", "claude"), 1)
        for identity in (None, "conflict"):
            record["sessionId"] = identity
            path.write_bytes(line(record))
            self.bump(path)
            self.run_ingest()
            with mock.patch.dict(os.environ, self.env, clear=True):
                result = hook_learn.capture_boundary(
                    self.home,
                    "claude",
                    "pre-compact",
                    json.dumps(payload).encode(),
                    self.policy,
                )
            self.assertFalse(result.accepted)

    def test_default_prompt_recall_stays_off(self) -> None:
        (self.home / "recall.off").touch()
        self.assertEqual(
            self.call_hook(
                "prompt",
                self.payload(prompt="record durable memory decisions"),
            ),
            {},
        )
        self.assertEqual(self.count("review_request"), 0)

    def cursor_payload(self, event: str = "sessionEnd") -> dict[str, Any]:
        """Create a temporary native database with one exact conversation."""
        database = self.tmp / "cursor.vscdb"
        self.env["MUNINN_CURSOR_DB"] = str(database)
        if not database.exists():
            with contextlib.closing(sqlite3.connect(database)) as conn, conn:
                conn.execute("CREATE TABLE cursorDiskKV(key TEXT,value TEXT)")
                conn.executescript("""
INSERT INTO cursorDiskKV VALUES
('composerData:active','{"composerId":"active","fullConversationHeadersOnly":[{"bubbleId":"one"},{"bubbleId":"two"}]}'),
('bubbleId:active:one','{"type":1,"text":"q"}'),
('bubbleId:active:two','{"type":2,"text":"a"}');
""")
        return {
            "hook_event_name": event,
            "conversation_id": "active",
            "session_id": "active",
            "generation_id": "generation",
            "model": "explicit",
            "cursor_version": "snapshot",
            "workspace_roots": [str(self.tmp)],
            "is_background_agent": False,
            "reason": "completed",
            "duration_ms": 100,
            "final_status": "completed",
        }

    def import_cursor(self) -> None:
        """Establish the direct-capture prerequisite using real storage."""
        cursor_import.run(
            self.home,
            self.tmp / "cursor.vscdb",
            str(self.tmp),
            0,
            conversation_id="active",
        )

    def test_cursor_end_without_message_count(self) -> None:
        payload = self.cursor_payload()
        real = cursor_import.run
        with mock.patch.object(cursor_import, "run", wraps=real) as imported:
            self.assertEqual(
                self.call_hook("session-end", payload, "cursor"), {}
            )
        self.assertEqual(imported.call_count, 1)
        self.assertIsNone(imported.call_args.kwargs["max_bubbles"])
        self.assertEqual(self.count("review_request", "cursor"), 2)

    def test_busy_cursor_end_reports_unrecovered_boundary(self) -> None:
        payload = self.cursor_payload()
        self.import_cursor()
        for failure in (
            store.BusyError("busy"),
            ValueError("private native content"),
        ):
            with (
                mock.patch.object(cursor_import, "run", side_effect=failure),
                mock.patch.object(hook_learn, "capture_boundary") as captured,
            ):
                self.assertEqual(
                    self.call_hook("session-end", payload, "cursor"), {}
                )
                captured.assert_not_called()
        self.assertEqual(self.count("review_request", "cursor"), 0)
        logs = (self.home / "calls.jsonl").read_text()
        self.assertNotIn("private native content", logs)
        self.assertIn('"missed_acknowledgments":1', logs)
        self.assertIn('"review_enqueue_busy"', logs)

    def test_cursor_zero_import_never_acknowledges_stale_source(self) -> None:
        payload = self.cursor_payload()
        self.import_cursor()
        payload.update(conversation_id="absent", session_id="absent")
        with mock.patch.object(hook_learn, "capture_boundary") as captured:
            self.call_hook("session-end", payload, "cursor")
            captured.assert_not_called()
        self.assertEqual(self.count("review_request"), 0)


class FailedCommit(sqlite3.Connection):
    """Inject a commit failure after real transactional request inserts."""

    def commit(self) -> None:
        """Fail before commit to expose premature acknowledgment."""
        raise sqlite3.OperationalError("injected commit failure")
