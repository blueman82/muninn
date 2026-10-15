"""Real recall rendering and the review CLI use final, linked evidence."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any
from unittest import mock

from muninn import cli, hook, recall_audit
from muninn import cli_recall_review as review_command
from muninn.cli_recall_review import _assessment
from tests.hook_support import AKIA, RecallCase


class RecallReviewTests(RecallCase):
    """User-visible assessment hints and request linkage in a temp HOME."""

    def reviewed(self, *args: str) -> tuple[int, dict[str, Any]]:
        """Run the review command and parse its JSON response."""
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env),
            contextlib.redirect_stdout(output),
        ):
            code = cli.main(["recall-review", *args])
        return code, json.loads(output.getvalue())

    def test_real_hook_records_only_sources_in_final_context(self) -> None:
        for index in range(3):
            self.talk(
                f"old{index}",
                "alphaterm betaterm gammaterm " + "padding " * 50,
            )
        self.noise(self.repo)
        trace: dict[str, Any] = {}
        out = hook.prompt_submit(
            {
                "cwd": "/repo",
                "session_id": "current",
                "prompt": "alphaterm betaterm gammaterm",
            },
            "codex",
            self.env,
            trace=trace,
        )
        text = self.body(out)
        self.assertIn("Memory R1: useful/noise/uncertain", text)
        self.assertLessEqual(len(text), hook.CODEX_RECALL_LIMIT)
        report = recall_audit.review(self.home, None, ack=False)
        sources = report["deliveries"][0]["sources"]
        self.assertEqual(len(sources), len(trace["delivered_ids"]))
        for source in sources:
            self.assertIn(source["ref"], text)
        self.assertLess(len(sources), len(trace["returned_ids"]))

    def test_off_and_empty_hooks_do_not_count(self) -> None:
        self.noise(self.repo)
        self.assertEqual(self.ask(), {})
        self.assertFalse((self.home / recall_audit.NAME).exists())
        self.talk("a", "alphaterm betaterm gammaterm")
        (self.home / "recall.off").touch()
        self.assertEqual(self.ask(), {})
        self.assertFalse((self.home / recall_audit.NAME).exists())

    def test_review_resolves_request_and_model_assessment(self) -> None:
        now = datetime.now(UTC).isoformat()
        number = self.talk("current", "alphaterm betaterm gammaterm", ts=now)
        row = self.rw.execute(
            "SELECT * FROM event WHERE id=?", (number,)
        ).fetchone()
        self.talk("old", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        request_ref = self.ref(number)
        session = self.rw.execute(
            "SELECT thread_id FROM source WHERE id=?", (row["source_id"],)
        ).fetchone()[0]
        hook.prompt_submit(
            {"cwd": "/repo", "session_id": session, "prompt": row["text"]},
            "codex",
            self.env,
        )
        assessment = self.add_event(
            row["source_id"],
            self.repo,
            "Memory R1: noise — unrelated evidence",
            kind="reply",
            role="assistant",
            ts=now,
        )
        code, report = self.reviewed()
        self.assertEqual(code, 0)
        delivery = report["deliveries"][0]
        self.assertEqual(delivery["request_ref"], request_ref)
        self.assertEqual(delivery["assessment"]["rating"], "noise")
        self.assertEqual(delivery["assessment"]["ref"], self.ref(assessment))
        self.assertIn("self-report", report["review_prompt"])

    def test_review_marks_missing_requests_unresolved(self) -> None:
        self.talk("old", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        self.ask()
        code, report = self.reviewed()
        self.assertEqual(code, 0)
        self.assertEqual(
            report["deliveries"][0]["request_status"], "unresolved"
        )
        self.assertEqual(
            report["deliveries"][0]["assessment"]["rating"], "unassessed"
        )
        self.assertEqual(self.reviewed("--ack")[0], 2)
        self.assertEqual(self.reviewed("--batch", "1", "--ack")[0], 2)

    def test_due_reminder_fits_both_provider_caps(self) -> None:
        self.talk("old", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                with mock.patch.object(
                    recall_audit,
                    "counts",
                    return_value=(49 if provider == "claude" else 99, 0),
                ):
                    out = hook.prompt_submit(
                        {
                            "cwd": "/repo",
                            "session_id": "now",
                            "prompt": "alphaterm betaterm gammaterm",
                        },
                        provider,
                        self.env,
                    )
                text = self.body(out)
                self.assertIn("Review due", text)
                self.assertIn("recall review due", str(out["systemMessage"]))
                self.assertIn(
                    "recall-review --batch 1", str(out["systemMessage"])
                )
                self.assertLessEqual(
                    len(text), 900 if provider == "codex" else 1500
                )

    def test_echoed_options_are_not_a_usefulness_rating(self) -> None:
        from_source = self.add_source("rating", session="rating")
        number = self.add_event(from_source, self.repo, "request")
        row = self.rw.execute(
            "SELECT * FROM event WHERE id=?", (number,)
        ).fetchone()
        self.add_event(
            from_source,
            self.repo,
            "Memory R1: useful/noise/uncertain — reason",
            kind="reply",
            role="assistant",
        )
        self.assertEqual(
            _assessment(self.ro(), row, 1)["rating"], "unassessed"
        )

    def test_legacy_request_and_reason_previews_are_redacted(self) -> None:
        prompt = f"alphaterm betaterm gammaterm {AKIA}"
        now = datetime.now(UTC).isoformat()
        number = self.talk("legacy", prompt, ts=now)
        source = self.rw.execute(
            "SELECT source_id FROM event WHERE id=?", (number,)
        ).fetchone()[0]
        self.talk("older", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        self.ask(prompt, provider="codex", session_id="legacy")
        self.add_event(
            source,
            self.repo,
            f"Memory R1: noise — {AKIA}",
            kind="reply",
            role="assistant",
            ts=now,
        )
        code, report = self.reviewed()
        self.assertEqual(code, 0)
        self.assertNotIn(AKIA, json.dumps(report))
        delivery = report["deliveries"][0]
        self.assertIn("[redacted:secret]", delivery["request_preview"])
        self.assertIn(
            "[redacted:secret]", delivery["assessment"]["reason_preview"]
        )

    def test_statement_lock_after_open_returns_json_error(self) -> None:
        self.talk("old", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        self.ask()
        request = review_command._request

        def locked(
            conn: sqlite3.Connection, delivery: dict[str, Any]
        ) -> sqlite3.Row | None:
            conn.execute("PRAGMA busy_timeout=1")
            self.rw.execute("BEGIN EXCLUSIVE")
            try:
                return request(conn, delivery)
            finally:
                self.rw.execute("ROLLBACK")

        with mock.patch.object(review_command, "_request", side_effect=locked):
            code, report = self.reviewed()
        self.assertEqual(code, 4)
        self.assertEqual(report["error"], "store_unavailable")
        self.assertIn("SQLITE_BUSY", report["detail"])
