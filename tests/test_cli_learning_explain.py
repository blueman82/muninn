"""Inspect synthetic learning failures without exposing retained content."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import cli, review_recovery, review_requests
from tests.test_cli_learning import CliLearningCase

SECRET = "private-evidence-and-proposal-canary"


class LearningExplanationTests(CliLearningCase):
    """Keep human inspection compatible with JSON and notice history."""

    def invoke(self, *argv: str) -> tuple[int, dict[str, Any], str, str]:
        """Capture both CLI streams with disposable homes and cwd."""
        output, errors = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "MUNINN_HOME": str(self.home),
                    "HOME": str(self.tmp / "userhome"),
                },
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            os.chdir(self.cwd)
            try:
                code = cli.main(argv)
            finally:
                os.chdir(previous)
        raw = output.getvalue()
        return code, json.loads(raw) if raw else {}, errors.getvalue(), raw

    def save_failure(self, code: str = "rejected_authority") -> str:
        """Save a real terminal attempt and optional safe notice."""
        ident = self.context.request_id
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, self.context.attempt_id, code, 20.0
            )
        )
        self.transaction(
            lambda: review_recovery.record(
                self.conn, ident, code, "review", 20.0
            )
        )
        return ident

    def test_human_stream_is_prose_and_stdout_remains_json(self) -> None:
        ident = self.save_failure()
        code, out, text, raw = self.invoke(
            "learn", "explain", ident, "--human"
        )
        self.assertEqual(code, 0)
        self.assertEqual(text.strip(), out["explanation"])
        self.assertEqual(len(raw.splitlines()), 1)
        self.assertIn("Nothing from this request was saved", text)
        self.assertIn("No model launch is recorded", text)
        self.assertIn("finer failure phase was not recorded", text)
        self.assertIn("safety check rejected", text)
        self.assertIn("rejected incorrectly", text)
        self.assertIn(f"muninn learn dismiss {ident}", text)
        self.assertNotIn("Learn", (self.home / "calls.jsonl").read_text())

    def test_pretty_only_changes_json_indentation(self) -> None:
        ident = self.save_failure()
        code, plain, stderr, raw = self.invoke("learn", "explain", ident)
        self.assertEqual((code, stderr, len(raw.splitlines())), (0, "", 1))
        code, pretty, stderr, raw = self.invoke(
            "learn", "explain", ident, "--pretty"
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertGreater(len(raw.splitlines()), 1)
        self.assertEqual(plain["explanation"], pretty["explanation"])

    def test_proposal_is_unavailable_and_content_is_withheld(
        self,
    ) -> None:
        ident = self.save_failure()
        self.conn.execute("UPDATE review_evidence SET excerpt=?", (SECRET,))
        self.conn.execute(
            "UPDATE review_issue SET cause=?,action=?", (SECRET, SECRET)
        )
        _, out, text, raw = self.invoke("learn", "explain", ident, "--human")
        self.assertNotIn(SECRET, raw + text)
        self.assertIn("Failed model proposals are not retained", text)
        self.assertIn("2 have active retained excerpts", text)
        self.assertIn("exact passage cited by a rejected proposal", text)
        self.assertNotIn(SECRET, (self.home / "calls.jsonl").read_text())
        self.assertEqual(out["request_id"], ident)

    def test_erased_changed_and_other_scope_text_is_withheld(
        self,
    ) -> None:
        ident = self.context.request_id
        self.conn.execute(
            "UPDATE review_evidence SET state='erased',excerpt=NULL"
        )
        _, out = self.call("learn", "explain", ident)
        self.assertIn("0 have active retained excerpts", out["explanation"])
        self.conn.execute(
            "UPDATE review_evidence SET state='changed',excerpt=?,"
            "scope_json=?",
            (
                SECRET,
                json.dumps({"project_id": "other-project", "task": None}),
            ),
        )
        _, out = self.call("learn", "explain", ident)
        self.assertNotIn(SECRET, out["explanation"])
        self.assertNotIn("other-project", out["explanation"])

    def test_legacy_config_does_not_promise_discarded_cause(self) -> None:
        ident = self.save_failure("config")
        self.call("learn", "dismiss", ident)
        _, out = self.call("learn", "explain", ident)
        text = out["explanation"]
        self.assertIn("historical attempt failure", text)
        self.assertIn("specific cause was not recorded", text)
        self.assertIn("old stage cannot recover the cause", text)
        self.assertNotIn("Inspect the recorded setup stage", text)

    def test_specific_limits_have_no_invented_counts(
        self,
    ) -> None:
        ident = self.save_failure("evidence_limit")
        for kind, phrase in (
            ("byte", "physical byte-read"),
            ("record", "physical record-read"),
            ("work", "database-work"),
            ("selection", "accumulated selected-content"),
        ):
            with self.subTest(kind=kind):
                self.conn.execute(
                    "UPDATE review_issue SET code=?",
                    (f"evidence_{kind}_limit",),
                )
                _, out = self.call("learn", "explain", ident)
                text = out["explanation"]
                self.assertIn(phrase, text)
                self.assertIn("Do not retry this deterministic", text)
                self.assertIn("maintainer must inspect", text)
                self.assertIn("No historical limit or measured counter", text)

    def test_unchanged_limit_failure_has_no_automatic_retry(
        self,
    ) -> None:
        ident = self.save_failure("evidence_limit")
        self.assertEqual(
            tuple(self.row("SELECT state,retry_at FROM review_request")),
            ("failed", None),
        )
        for _ in range(2):
            self.assertEqual(self.call("learn", "explain", ident)[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_attempt")[0], 1)

    def test_paused_admission_is_separate_from_old_failure(
        self,
    ) -> None:
        ident = self.save_failure()
        self.conn.execute("UPDATE review_control SET admission=0")
        _, out = self.call("learn", "explain", ident)
        text = out["explanation"]
        self.assertIn("Current learning admission is paused", text)
        self.assertIn("does not identify the current cause", text)
        self.assertNotIn("Unrelated requests can continue", text)

    def test_setup_priority_does_not_claim_live_failure(
        self,
    ) -> None:
        ident = self.save_failure()
        self.transaction(
            lambda: review_recovery.record(
                self.conn, "setup", "auth", "launch", 21.0
            )
        )
        _, out = self.call("learn", "explain", ident)
        text = out["explanation"]
        self.assertIn("separate saved setup notice takes priority", text)
        self.assertIn("Open Codex and sign in", text)
        self.assertIn("does not prove the setup still fails", text)

    def test_success_clears_notice_and_reports_saved_impact(
        self,
    ) -> None:
        ident = self.context.request_id
        self.transaction(
            lambda: review_recovery.record(
                self.conn, ident, "rejected_authority", "review", 12.5
            )
        )
        self.assertEqual(self.propose()[0], 0)
        self.assertIsNone(review_recovery.notice(self.conn))
        _, out = self.call("learn", "explain", ident)
        text = out["explanation"]
        self.assertIn("currently completed", text)
        self.assertIn("1 applied knowledge changes", text)
        self.assertIn("No failure is recorded", text)

    def test_dismiss_reveals_older_notice_then_new_failure(
        self,
    ) -> None:
        ident = self.save_failure()
        other = self.enqueue(self.window(2, 2)).id
        self.transaction(
            lambda: review_recovery.record(
                self.conn, other, "rejected_scope", "review", 19.0
            )
        )
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], ident)
        self.call("learn", "dismiss", ident)
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], other)
        self.transaction(
            lambda: review_recovery.record(
                self.conn, ident, "rejected_scope", "review", 22.0
            )
        )
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], ident)
        _, out = self.call("learn", "explain", ident)
        self.assertIn("Dismissing the newest notice", out["explanation"])

    def test_optional_legacy_table_and_unknown_request(self) -> None:
        self.conn.execute("DROP TABLE review_issue")
        self.assertEqual(
            self.call("learn", "explain", self.context.request_id)[0], 0
        )
        code, out = self.call("learn", "explain", "R_" + "a" * 32)
        self.assertEqual((code, out["error"]), (2, "request_unavailable"))
        self.assertEqual(self.call("learn", "explain", "setup")[0], 2)

    def test_correction_field_is_unknown_for_unmarked_history(self) -> None:
        ident = self.context.request_id
        _, shown = self.call("learn", "show", ident)
        _, explained = self.call("learn", "explain", ident)
        self.assertEqual(shown.get("correction_status"), "unknown")
        self.assertEqual(explained.get("correction_status"), "unknown")
        self.assertIn("Correction status: unknown", explained["explanation"])
        self.assertIn("does not prove", explained["explanation"])

    def test_each_observed_failure_phase_has_a_human_description(self) -> None:
        ident = self.save_failure("rejected_validation")
        for stage, phrase in (
            ("native output", "while checking the native runner output"),
            ("model format", "while checking the model result format"),
            ("candidate validation", "while validating proposed candidates"),
            ("controlled read", "while performing a controlled read"),
            ("original evidence", "while checking original source evidence"),
            ("commit", "while committing the checked result"),
        ):
            with self.subTest(stage=stage):
                self.conn.execute("UPDATE review_issue SET stage=?", (stage,))
                _, out = self.call("learn", "explain", ident)
                self.assertIn(phrase, out["explanation"])
                self.assertNotIn(
                    "stage that was not recorded", out["explanation"]
                )

    def test_successful_correction_survives_cleared_notice(self) -> None:
        ident = self.context.request_id
        for index, outcome in (
            (1, "correction_format_requested"),
            (2, "completed"),
        ):
            self.conn.execute(
                (
                    "INSERT INTO "
                    "review_call(attempt_id,round_index,budget_account_id,"
                    "policy_digest,state,reserved_at,launched_at,ended_at,"
                    "requested_json,observed_json,outcome) "
                    "VALUES(?,?,'test',?,'completed',12,12.5,13,'{}','{}',?)"
                ),
                (
                    self.context.attempt_id,
                    index,
                    self.digest(self.policy),
                    outcome,
                ),
            )
        self.transaction(
            lambda: review_recovery.record(
                self.conn, ident, "rejected_validation", "model format", 12.5
            )
        )
        self.save(self.result([]))
        self.assertIsNone(review_recovery.notice(self.conn))
        _, out = self.call("learn", "explain", ident)
        self.assertEqual(out.get("correction_status"), "succeeded")
        self.assertIn("Correction status: succeeded", out["explanation"])
        self.assertIn("0 applied knowledge changes", out["explanation"])
        self.assertIn("no_change", out["explanation"])
