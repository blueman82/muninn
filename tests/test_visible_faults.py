"""Doctor, stats and the hook note surface the poller's failures."""

from __future__ import annotations

import json
import time
import unittest
from functools import partial
from typing import Any
from unittest import mock

from muninn import (
    cli,
    ingest_plan,
    obs,
    obs_stats,
    review_budget,
    review_progress,
    review_requests,
)
from muninn.review_models import ReviewContext
from tests.cli_support import CliCase, fake_run
from tests.hook_support import HookCase
from tests.ingest_support import IngestCase, primary
from tests.test_obs import ReviewReceiptCase

ERR_NOTE = "muninn: the last pass stopped with"
FILE_NOTE = "2 failed transcript open attempt(s)"


class DoctorFaultTests(CliCase):
    """The doctor checks for a failed pass and unreadable files."""

    def checks(self) -> dict[str, dict[str, Any]]:
        """Run doctor with a healthy launchd job; map check name to result."""
        with mock.patch.object(cli.obs, "run", fake_run()):
            _, out, _ = self.muninn("doctor")
        return {c["check"]: c for c in out["checks"]}

    def test_clean_status_passes_both(self) -> None:
        got = self.checks()
        self.assertTrue(got["poller_error"]["ok"])
        self.assertTrue(got["unreadable_files"]["ok"])

    def test_error_class_and_count_are_reported_as_warnings(self) -> None:
        obs.write_status(
            self.home, {"last_error": "OSError", "unreadable_files": 2}
        )
        got = self.checks()
        self.assertEqual(got["poller_error"]["detail"], "OSError")
        self.assertIs(got["poller_error"]["ok"], False)
        self.assertEqual(got["poller_error"]["level"], "warn")
        self.assertEqual(got["unreadable_files"]["detail"], "2")
        self.assertEqual(got["unreadable_files"]["level"], "warn")

    def test_text_in_last_error_is_never_echoed(self) -> None:
        text = "/Users/me/secret path: boom"
        obs.write_status(self.home, {"last_error": text})
        got = self.checks()["poller_error"]
        self.assertTrue(got["ok"])
        self.assertNotIn("secret", got["detail"])
        _, out, _ = self.muninn("stats")
        self.assertIsNone(out["last_pass"]["last_error"])

    def test_stats_shows_the_class_name_and_the_count(self) -> None:
        obs.write_status(
            self.home, {"last_error": "OSError", "unreadable_files": 1}
        )
        _, out, _ = self.muninn("stats")
        self.assertEqual(out["last_pass"]["last_error"], "OSError")
        self.assertEqual(out["last_pass"]["unreadable_files"], 1)

    def test_stats_review_is_compact_durable_and_nullable(self) -> None:
        _, out, _ = self.muninn("stats")
        review = out["review"]
        self.assertEqual(review["schema_version"], 1)
        self.assertEqual(review["requests_by_state"], {})
        self.assertEqual(review["completed_records"], 0)
        self.assertIsNone(review["usage"]["input_tokens"])
        self.assertIs(review["busy_count_complete"], False)
        self.assertNotIn("knowledge_restricted", out)
        self.assertIn("evidence", out)

    def test_doctor_exposes_review_health_without_repairing(self) -> None:
        got = self.checks()
        self.assertIn("review_capture_gaps", got)
        self.assertIn("review_process_deadline", got)
        self.assertIsNone(got["review_pause"]["ok"])
        self.assertFalse((self.home / "review.supervisor.json").exists())


class HookFaultTests(HookCase):
    """The hook note for a failed pass and unreadable files."""

    def test_unreadable_files_add_a_note(self) -> None:
        obs.write_status(self.home, {"unreadable_files": 2})
        self.assertIn(FILE_NOTE, self.body(self.start_hook()))

    def test_no_note_for_zero_or_junk_counts(self) -> None:
        for junk in (0, True, -1, "2", None):
            with self.subTest(n=junk):
                obs.write_status(self.home, {"unreadable_files": junk})
                self.assertNotIn(
                    "failed transcript open attempt(s)",
                    self.body(self.start_hook()),
                )

    def test_a_recent_error_adds_a_note_with_the_class_only(self) -> None:
        obs.write_status(
            self.home,
            {"last_error": "OSError", "last_error_at": time.time()},
        )
        text = self.body(self.start_hook())
        self.assertIn(f"{ERR_NOTE} OSError", text)

    def test_an_error_older_than_the_last_pass_adds_no_note(self) -> None:
        now = time.time()
        obs.write_status(
            self.home,
            {
                "last_error": "OSError",
                "last_error_at": now - 100,
                "last_pass_at": now,
            },
        )
        self.assertNotIn(ERR_NOTE, self.body(self.start_hook()))

    def test_a_malformed_error_adds_no_note(self) -> None:
        obs.write_status(self.home, {"last_error": "see /tmp/x: boom"})
        self.assertNotIn(ERR_NOTE, self.body(self.start_hook()))


class UnreadableFileTests(IngestCase):
    """Only a file that cannot be opened counts as unreadable."""

    def plan_raises(self, exc: OSError) -> Any:
        """Run a pass over one transcript whose planning raises ``exc``."""
        self.write("p/a.jsonl", primary(), root="claude-projects")
        with mock.patch.object(ingest_plan, "_plan", side_effect=exc):
            return self.run_ingest()

    def test_a_permission_error_is_counted(self) -> None:
        stats = self.plan_raises(PermissionError("denied"))
        self.assertEqual(stats.unreadable_files, 1)
        self.assertEqual(stats.skipped_files, 1)

    def test_a_vanished_file_is_only_skipped(self) -> None:
        stats = self.plan_raises(FileNotFoundError("gone"))
        self.assertEqual(stats.unreadable_files, 0)
        self.assertEqual(stats.skipped_files, 1)


if __name__ == "__main__":
    unittest.main()


class ReviewHistoricalGapTests(ReviewReceiptCase):
    """Use identical eligibility for explicit historical health and counts."""

    def test_historical_pending_failed_cancelled_and_receipted(self) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        before = obs_stats.review_stats(self.conn, 20.0)
        self.assertEqual(
            (before.eligible_records, before.completed_records), (0, 0)
        )
        self.assertTrue(
            next(
                c
                for c in obs.review_checks(self.conn, 20.0)
                if c["check"] == "review_coverage_gaps"
            )["ok"]
        )
        request = self.enqueue()
        for state in ("pending", "failed", "cancelled"):
            if state == "failed":
                attempt = self.transaction(
                    lambda: review_requests.claim_attempt(
                        self.conn, request.id, 1, 12.0
                    )
                )
                self.transaction(
                    partial(
                        review_requests.fail_attempt,
                        self.conn,
                        attempt.id,
                        "auth",
                        13.0,
                    )
                )
            elif state == "cancelled":
                self.transaction(
                    lambda: review_requests.cancel_request(
                        self.conn, request.id, 14.0
                    )
                )
            for name in ("calls.jsonl", "calls.jsonl.1"):
                (self.home / name).unlink(missing_ok=True)
            got = obs_stats.review_stats(self.conn, 20.0)
            self.assertEqual(
                (got.eligible_records, got.completed_records), (2, 0)
            )
            self.assertIs(
                next(
                    c
                    for c in obs.review_checks(self.conn, 20.0)
                    if c["check"] == "review_coverage_gaps"
                )["ok"],
                False,
            )
        self.transaction(
            lambda: review_requests.retry_request(self.conn, request.id, 15.0)
        )
        self.receipt(request.id)
        self.assertEqual(
            obs_stats.review_stats(self.conn, 20.0).completed_records, 2
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT activation_line FROM review_source"
            ).fetchone()[0],
            3,
        )


class ReviewUsageStateTests(ReviewReceiptCase):
    """Keep diagnostic actual usage unknown for unconcluded durable calls."""

    def reserve(self) -> str:
        """Claim and reserve a real request using an explicit policy."""
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        context = ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=request.scope,
            policy=self.policy,
            admitted_windows=(request.window,),
            evidence_ids=(),
            round_index=1,
            deadline=72.0,
        )
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 13.0)
        )
        return attempt.id

    def test_unconcluded_calls_with_retained_usage_stay_unknown(self) -> None:
        self.reserve()
        usage = {
            "input_tokens": 3,
            "output_tokens": 2,
            "cached_tokens": 0,
            "cost_decimal": "0.1",
            "currency": "EUR",
        }
        for state in ("reserved", "launched", "unknown"):
            self.conn.execute(
                "UPDATE review_call SET state=?,usage_json=?",
                (state, json.dumps(usage)),
            )
            got = obs_stats.review_stats(self.conn, 20.0).usage
            self.assertTrue(
                all(value is None for value in got.model_dump().values())
            )

    def test_unsafe_currency_is_not_health_text_or_known_cost(self) -> None:
        self.reserve()
        usage = {
            "input_tokens": 3,
            "output_tokens": 2,
            "cached_tokens": 0,
            "cost_decimal": "0.1",
            "currency": "private-result-and-exception-canary",
        }
        self.conn.execute(
            "UPDATE review_call SET state='completed',usage_json=?",
            (json.dumps(usage),),
        )
        got = obs_stats.review_stats(self.conn, 20.0).usage
        self.assertEqual(got.input_tokens, 3)
        self.assertIsNone(got.currency)
        self.assertIsNone(got.cost_decimal)
