"""Select actionable startup notices without changing saved review history."""

from __future__ import annotations

import contextlib
import io
import json
import os
from unittest import mock

from muninn import cli_hook, hook, obs, review_recovery, review_requests
from tests.ingest_support import TID
from tests.test_cli_learning import CliLearningCase


class LearningStartupActionTests(CliLearningCase):
    """Read real SQLite state through the SessionStart and inspection paths."""

    def save_issue(
        self, code: str, ident: str | None = None, now: float = 20.0
    ) -> str:
        """Persist a finite notice independently of its display eligibility."""
        request_id = self.context.request_id if ident is None else ident
        self.transaction(
            lambda: review_recovery.record(
                self.conn, request_id, code, "review", now
            )
        )
        return request_id

    def startup(self) -> dict[str, object]:
        """Read startup output from the mapped project and isolated store."""
        return hook.session_start(
            {"session_id": TID, "cwd": str(self.cwd)},
            "codex",
            {"MUNINN_HOME": str(self.home)},
        )

    def test_startup_keeps_older_action_required_issue(self) -> None:
        older = self.save_issue("auth")
        newer = self.enqueue(self.window(2, 2))
        self.save_issue("rejected_authority", newer.id, 30.0)
        self.assertIn(older, str(self.startup().get("systemMessage")))
        self.assertNotIn(newer.id, str(self.startup().get("systemMessage")))
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], newer.id)

    def test_unsafe_admission_keeps_older_required_action(self) -> None:
        older = self.save_issue("auth")
        newer = self.enqueue(self.window(2, 2))
        self.save_issue("rejected_validation", newer.id, 30.0)
        self.conn.execute("UPDATE review_control SET admission=0")
        text = str(self.startup().get("systemMessage"))
        self.assertIn(older, text)
        self.assertIn("sign in", text)
        self.assertIn("paused", text)
        self.assertNotIn(newer.id, text)
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], newer.id)

    def test_contained_failures_are_quiet_only_with_safe_admission(
        self,
    ) -> None:
        for code in (
            "deadline",
            "unavailable",
            "rejected_revision",
            "rejected_authority",
            "rejected_validation",
            "rejected_evidence",
            "rejected_erased",
            "rejected_scope",
            "evidence_limit",
            "evidence_byte_limit",
            "evidence_record_limit",
            "evidence_work_limit",
            "evidence_selection_limit",
        ):
            with self.subTest(code=code):
                self.save_issue(code)
                self.assertNotIn("Muninn learning", str(self.startup()))
                self.conn.execute("UPDATE review_control SET admission=0")
                self.assertIn("paused", str(self.startup()))
                self.conn.execute("UPDATE review_control SET admission=1")

    def test_required_action_codes_remain_visible(self) -> None:
        for code in (
            "auth",
            "launch_input_binding",
            "native_tool_event",
            "process_identity_unverified",
            "capability_changed",
            "capability_unverified",
            "capability_config",
            "capability_storage",
            "capability_executable_provenance",
            "effective_host_changed",
            "config",
            "approval_required",
            "policy_invalid",
            "owner_pause",
            "future_unknown_failure",
        ):
            with self.subTest(code=code):
                ident = self.save_issue(code)
                # Unknown saved codes must fail toward owner attention.
                self.conn.execute("UPDATE review_issue SET code=?", (code,))
                self.assertIn(ident, str(self.startup().get("systemMessage")))

    def test_setup_priority_applies_after_containment_filter(self) -> None:
        ident = self.save_issue("auth", now=30.0)
        self.save_issue("launch_input_binding", "setup", 10.0)
        text = str(self.startup().get("systemMessage"))
        self.assertIn("does not match the installed policy", text)
        self.assertNotIn(ident, text)
        self.save_issue("rejected_validation", "setup", 40.0)
        self.assertIn("Muninn learning", str(self.startup()))

    def test_closed_admission_without_saved_issue_is_visible(self) -> None:
        self.conn.execute("DELETE FROM review_issue")
        self.conn.execute("UPDATE review_control SET admission=0")
        before = list(self.conn.iterdump())
        self.assertIn("paused", str(self.startup().get("systemMessage")))
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertIsNone(review_recovery.notice(self.conn))

    def test_policy_integrity_failure_without_saved_issue_is_visible(
        self,
    ) -> None:
        for field, value in (
            ("policy_digest", "0" * 64),
            ("policy_json", "{}"),
            ("policy_json", None),
        ):
            with self.subTest(field=field):
                self.conn.execute(
                    f"UPDATE review_control SET {field}=?", (value,)
                )
                self.assertIn("Muninn learning", str(self.startup()))
                self.assertIn("muninn doctor --pretty", str(self.startup()))

    def test_current_admission_overrides_stale_saved_action(self) -> None:
        self.conn.execute("UPDATE review_control SET admission=0")
        self.save_issue("rejected_authority")
        self.conn.execute("UPDATE review_control SET admission=1")
        self.assertNotIn("Muninn learning", str(self.startup()))
        self.save_issue("auth")
        self.conn.execute("UPDATE review_control SET admission=0")
        self.assertIn("paused", str(self.startup()))

    def test_repeated_startup_preserves_all_history_and_withholds_content(
        self,
    ) -> None:
        ident = self.save_issue("auth")
        canaries = ("candidate-secret", "question-secret", "source-secret")
        self.conn.execute(
            "UPDATE review_evidence SET excerpt=?", (canaries[2],)
        )
        self.conn.execute(
            "UPDATE review_issue SET cause=?,action=?,stage=?",
            (canaries[0], canaries[1], canaries[2]),
        )
        self.conn.execute(
            "INSERT INTO unresolved VALUES(?,?,?,'[]','[]',20.0,'open')",
            ("U_" + "b" * 32, ident, json.dumps({"question": canaries[1]})),
        )
        before = list(self.conn.iterdump())
        for _ in range(3):
            trace: dict[str, object] = {}
            out = hook.session_start(
                {"session_id": TID, "cwd": str(self.cwd)},
                "codex",
                {"MUNINN_HOME": str(self.home)},
                trace=trace,
            )
            text = json.dumps(out) + json.dumps(trace)
            self.assertIn(ident, text)
            for canary in canaries:
                self.assertNotIn(canary, text)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_native_hooks_are_compact_and_exit_zero_for_filter_and_pause(
        self,
    ) -> None:
        ident = self.save_issue("rejected_validation")
        canaries = (
            "candidate-native-secret",
            "question-native-secret",
            "source-native-secret",
        )
        self.conn.execute(
            "UPDATE review_evidence SET excerpt=?", (canaries[2],)
        )
        self.conn.execute(
            "INSERT INTO unresolved VALUES(?,?,?,'[]','[]',20.0,'open')",
            (
                "U_" + "a" * 32,
                ident,
                json.dumps({"question": canaries[1], "reason": canaries[0]}),
            ),
        )
        env = {
            "HOME": str(self.tmp / "native-home"),
            "MUNINN_HOME": str(self.home),
        }
        payload = json.dumps(
            {
                "session_id": TID,
                "cwd": str(self.cwd),
                "transcript_path": str(self.path),
                "hook_event_name": "SessionStart",
                "source": "startup",
                "model": "explicit",
                "permission_mode": "default",
            }
        )
        for provider in ("codex", "claude"):
            for admission in (1, 0):
                with self.subTest(provider=provider, admission=admission):
                    self.conn.execute(
                        "UPDATE review_control SET admission=?", (admission,)
                    )
                    output = io.StringIO()
                    stream = io.TextIOWrapper(io.BytesIO(payload.encode()))
                    with (
                        mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(cli_hook.sys, "stdin", stream),
                        contextlib.redirect_stdout(output),
                    ):
                        code = cli_hook.hook_main(
                            ["session-start", "--provider", provider], env
                        )
                    self.assertEqual(code, 0)
                    raw = output.getvalue()
                    self.assertEqual(len(raw.splitlines()), 1)
                    out = json.loads(raw)
                    self.assertEqual(
                        raw.strip(), json.dumps(out, separators=(",", ":"))
                    )
                    self.assertIn("hookSpecificOutput", out)
                    for canary in canaries:
                        self.assertNotIn(canary, raw)
                    if admission:
                        self.assertNotIn("Muninn learning", str(out))
                    else:
                        self.assertIn("paused", str(out.get("systemMessage")))
        safe_outputs = (
            json.dumps(obs.review_checks(self.conn, 21.0))
            + (self.home / "calls.jsonl").read_text()
        )
        for canary in canaries:
            self.assertNotIn(canary, safe_outputs)

    def test_contained_failure_remains_in_doctor_and_history(self) -> None:
        ident = self.context.request_id
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, self.context.attempt_id, "rejected_validation", 20.0
            )
        )
        for index, outcome in (
            (1, "correction_format_requested"),
            (2, "rejected_validation"),
        ):
            self.conn.execute(
                "INSERT INTO review_call(attempt_id,round_index,"
                "budget_account_id,policy_digest,state,reserved_at,launched_at,"
                "ended_at,requested_json,observed_json,outcome) "
                "VALUES(?,?,'test',?,'completed',12,12.5,13,?,?,?)",
                (
                    self.context.attempt_id,
                    index,
                    self.digest(self.policy),
                    self.policy.runner.model_dump_json(),
                    json.dumps(
                        {
                            "runner": None,
                            "version": None,
                            "model": None,
                            "reasoning": None,
                        }
                    ),
                    outcome,
                ),
            )
        self.save_issue("rejected_validation")
        _, before = self.call("learn", "show", ident)
        self.assertEqual(before["correction_status"], "exhausted")
        self.assertNotIn("Muninn learning", str(self.startup()))
        checks = {
            row["check"]: row for row in obs.review_checks(self.conn, 21.0)
        }
        self.assertIs(checks["review_attention"]["ok"], False)
        self.assertEqual(self.call("learn", "show", ident)[1], before)
        self.assertEqual(
            self.call("learn", "list", "--state", "failed")[1]["requests"][0][
                "id"
            ],
            ident,
        )
        self.assertEqual(
            self.call("learn", "dismiss", ident)[1]["dismissed"], True
        )
        self.assertEqual(
            self.call("learn", "dismiss", ident)[1]["dismissed"], False
        )
        _, after = self.call("learn", "show", ident)
        for field in (
            "request",
            "attempts",
            "calls",
            "evidence",
            "receipt",
            "correction_status",
        ):
            self.assertEqual(after[field], before[field])
        self.assertIsNone(review_recovery.notice(self.conn))
