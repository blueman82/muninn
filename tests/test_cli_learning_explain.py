"""Inspect synthetic learning failures without exposing retained content."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import cli, review_requests, review_settings
from tests.test_cli_learning import CliLearningCase


class LearningExplanationTests(CliLearningCase):
    """Keep human inspection compatible with JSON output."""

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
        """Save a real terminal attempt that carries the failure code."""
        ident = self.context.request_id
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, self.context.attempt_id, code, 20.0
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
        self.assertIn("rejected_authority", text)
        self.assertIn("lacks verified owner evidence", text)
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

    def test_unknown_code_gets_the_generic_sentence(self) -> None:
        self.conn.execute(
            "UPDATE review_attempt SET error_code='no_such_code',"
            "outcome='failed',ended_at=20"
        )
        _, out = self.call("learn", "explain", self.context.request_id)
        self.assertIn("no safe cause", out["explanation"])

    def test_learning_off_is_stated(self) -> None:
        ident = self.save_failure()
        review_settings.save(self.home, review_settings.Settings())
        _, out = self.call("learn", "explain", ident)
        self.assertIn("Learning is off", out["explanation"])

    def test_unknown_request_is_refused(self) -> None:
        code, out = self.call("learn", "explain", "R_" + "a" * 32)
        self.assertEqual((code, out["error"]), (2, "request_unavailable"))
        self.assertEqual(self.call("learn", "explain", "setup")[0], 2)

    def test_success_reports_saved_impact(self) -> None:
        ident = self.context.request_id
        self.assertEqual(self.propose()[0], 0)
        _, out = self.call("learn", "explain", ident)
        text = out["explanation"]
        self.assertIn("currently completed", text)
        self.assertIn("1 applied knowledge changes", text)
        self.assertIn("No failure is recorded", text)

    def test_correction_field_is_unknown_for_unmarked_history(self) -> None:
        ident = self.context.request_id
        _, shown = self.call("learn", "show", ident)
        _, explained = self.call("learn", "explain", ident)
        self.assertEqual(shown.get("correction_status"), "unknown")
        self.assertEqual(explained.get("correction_status"), "unknown")
        self.assertIn("Correction status: unknown", explained["explanation"])

    def test_successful_correction_is_reported(self) -> None:
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
        self.save(self.result([]))
        _, out = self.call("learn", "explain", ident)
        self.assertEqual(out.get("correction_status"), "succeeded")
        self.assertIn("0 applied knowledge changes", out["explanation"])
        self.assertIn("no_change", out["explanation"])
