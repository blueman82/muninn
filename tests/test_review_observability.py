"""Exercise durable review health against disposable normalized stores."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

from muninn import (
    obs,
    obs_stats,
    review_budget,
    review_progress,
    review_requests,
    store,
)
from muninn.review_models import ReviewContext
from tests.ingest_support import TID
from tests.test_obs import ReviewReceiptCase

SECRET = "private-result-and-exception-canary"


class ReviewObservabilityTests(ReviewReceiptCase):
    """Keep durable coverage separate from best-effort acknowledgments."""

    def snapshot(self, now: float = 20.0) -> dict[str, object]:
        """Read only the contracted review report at an explicit time."""
        return obs_stats.review_stats(self.conn, now).model_dump(mode="json")

    def checks(self, now: float = 20.0) -> dict[str, obs.CheckResult]:
        """Read content-free review diagnostics without changing state."""
        return {c["check"]: c for c in obs.review_checks(self.conn, now)}

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

    def test_prompt_limit_log_preserves_only_finite_failure(self) -> None:
        self.assertTrue(
            obs.log_call(
                self.home,
                {
                    "event": "review_attempt_failed",
                    "error_code": "prompt_limit",
                    "correction_baseline": SECRET,
                },
            )
        )
        line = (self.home / "calls.jsonl").read_text().splitlines()[-1]
        self.assertEqual(json.loads(line)["error_code"], "prompt_limit")
        self.assertNotIn(SECRET, line)
        self.assertNotIn("correction_baseline", line)

    def test_durable_counts_survive_log_rotation(self) -> None:
        self.reserve()
        before = self.snapshot()
        self.assertEqual(before["requests_by_state"], {"running": 1})
        self.assertEqual(before["calls_by_state"], {"reserved": 1})
        self.assertEqual(before["last_progress_age_seconds"], 8.0)
        for name in ("calls.jsonl", "calls.jsonl.1"):
            (self.home / name).unlink(missing_ok=True)
        with store.connect_ro(store.db_path(self.home)) as conn:
            self.addCleanup(conn.close)
            after = obs_stats.review_stats(conn, 20.0).model_dump(mode="json")
        self.assertEqual(before, after)

    def test_busy_ack_and_recovered_capture_are_separate(self) -> None:
        obs.log_call(self.home, {"event": "review_enqueue_busy"})
        request = self.enqueue()
        first = self.snapshot()
        self.assertEqual(first["observed_enqueue_busy"], 1)
        self.assertIs(first["busy_count_complete"], False)
        self.assertEqual(first["completed_records"], 0)
        self.receipt(request.id)
        second = self.snapshot()
        self.assertEqual(second["completed_records"], 2)
        self.assertEqual(second["observed_enqueue_busy"], 1)
        for name in ("calls.jsonl", "calls.jsonl.1"):
            (self.home / name).unlink(missing_ok=True)
        self.assertIsNone(self.snapshot()["observed_enqueue_busy"])
        self.assertEqual(self.snapshot()["completed_records"], 2)

    def test_null_usage_not_zero(self) -> None:
        self.reserve()
        self.assertEqual(
            self.snapshot()["usage"],
            {
                "input_tokens": None,
                "output_tokens": None,
                "cached_tokens": None,
                "cost_decimal": None,
                "currency": None,
            },
        )
        usage = {
            "input_tokens": 3,
            "output_tokens": 0,
            "cached_tokens": None,
            "cost_decimal": "0.10",
            "currency": "EUR",
        }
        self.conn.execute(
            "UPDATE review_call SET state='completed',usage_json=?",
            (json.dumps(usage),),
        )
        got = self.snapshot()["usage"]
        assert isinstance(got, dict)
        self.assertEqual(got, usage)
        self.assertEqual(Decimal(str(got["cost_decimal"])), Decimal("0.10"))

    def test_sensitive_exception_and_result_never_logged(self) -> None:
        obs.log_call(
            self.home,
            {
                "event": "review_attempt_failed",
                "error_code": SECRET,
                "result": SECRET,
                "error": SECRET,
                "boot": SECRET,
            },
        )
        self.assertNotIn(SECRET, (self.home / "calls.jsonl").read_text())
        self.reserve()
        self.conn.execute("UPDATE review_attempt SET error_code=?", (SECRET,))
        self.assertNotIn(SECRET, json.dumps(self.checks()))
        self.assertNotIn(SECRET, json.dumps(self.snapshot()))

    def test_overdue_attempt_and_archive_residue_visible(self) -> None:
        self.reserve()
        checks = self.checks(1000.0)
        self.assertIs(checks["review_process_deadline"]["ok"], False)
        self.conn.execute(
            "UPDATE review_call SET ended_at=90.0,state='completed'"
        )
        checks = self.checks(1000.0)
        self.assertIs(checks["review_process_deadline"]["ok"], True)
        self.assertIs(checks["review_commit_delay"]["ok"], False)
        archive = self.home / f"{store.PRE_UPGRADE_PREFIX}fixture"
        archive.touch(mode=0o600)
        with mock.patch.object(
            obs, "run", return_value=mock.Mock(returncode=1)
        ):
            got = obs.doctor(self.home, {"HOME": str(self.home)})
        mapped = {c["check"]: c for c in got["checks"]}
        self.assertIs(mapped["upgrade_snapshot"]["ok"], False)

    def test_baseline_gap_replay_and_refusal_remain_honest(self) -> None:
        self.conn.execute(
            "INSERT INTO review_source VALUES('codex',?,1,1,1,1,NULL,10.0)",
            (TID,),
        )
        self.assertEqual(self.snapshot()["completed_records"], 0)
        later = self.enqueue(self.window(3, 3))
        self.receipt(later.id)
        got = self.snapshot()
        self.assertEqual(
            (got["eligible_records"], got["completed_records"]), (2, 1)
        )
        self.assertIs(self.checks()["review_coverage_gaps"]["ok"], False)
        self.conn.execute(
            "UPDATE review_source SET eligible_line=1,examined_line=1"
        )
        for name in ("calls.jsonl", "calls.jsonl.1"):
            (self.home / name).unlink(missing_ok=True)
        self.assertIs(self.checks()["review_capture_gaps"]["ok"], False)
        self.assertEqual(self.snapshot()["completed_records"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT activation_line FROM review_source"
            ).fetchone()[0],
            1,
        )

    def test_pending_age_and_clock_skew_are_nonnegative(self) -> None:
        self.enqueue()
        self.assertEqual(self.snapshot()["oldest_pending_age_seconds"], 9.0)
        self.assertEqual(self.snapshot(1.0)["oldest_pending_age_seconds"], 0.0)
        self.assertIsNone(self.snapshot()["last_progress_age_seconds"])

    def test_overlap_multipart_and_historical_replay_are_counted_once(
        self,
    ) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        self.assertEqual(
            (
                self.snapshot()["eligible_records"],
                self.snapshot()["completed_records"],
            ),
            (0, 0),
        )
        # Two indexed parts of the same physical prompt remain one record.
        columns = [
            row[1]
            for row in self.conn.execute("PRAGMA table_info(event)")
            if row[1] != "id"
        ]
        projection = ",".join(
            "part+10" if name == "part" else name for name in columns
        )
        self.conn.execute(
            f"INSERT INTO event({','.join(columns)}) "
            f"SELECT {projection} FROM event WHERE kind='prompt'"
        )
        first = self.enqueue(self.window(2, 2))
        overlapping = self.enqueue(self.window(2, 3))
        self.receipt(first.id)
        self.receipt(overlapping.id)
        self.assertEqual(
            (
                self.snapshot()["eligible_records"],
                self.snapshot()["completed_records"],
            ),
            (2, 2),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT activation_line FROM review_source"
            ).fetchone()[0],
            3,
        )
        changes = self.conn.total_changes
        self.checks()
        self.snapshot()
        self.assertEqual(self.conn.total_changes, changes)

    def test_failed_and_cancelled_windows_do_not_fabricate_completion(
        self,
    ) -> None:
        first = self.enqueue(self.window(2, 2))
        second = self.enqueue(self.window(3, 3))
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(self.conn, first.id, 1, 12.0)
        )
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, attempt.id, "auth", 13.0
            )
        )
        self.transaction(
            lambda: review_requests.cancel_request(self.conn, second.id, 14.0)
        )
        self.assertEqual(self.snapshot()["completed_records"], 0)
        self.assertIs(self.checks()["review_auth"]["ok"], False)
        self.conn.execute(
            "UPDATE review_attempt SET usage_json=?",
            (json.dumps({"error": SECRET}),),
        )
        self.assertNotIn(SECRET, json.dumps(self.snapshot()))

    def test_usage_malformed_currency_and_partial_components_stay_unknown(
        self,
    ) -> None:
        self.reserve()
        self.conn.execute(
            "UPDATE review_call SET state='completed',usage_json=?",
            (json.dumps({"result": SECRET}),),
        )
        self.assertIsNone(
            obs_stats.review_stats(self.conn, 20.0).usage.input_tokens
        )
        self.conn.execute(
            "UPDATE review_call SET state='completed',usage_json=?",
            (
                json.dumps(
                    {
                        "input_tokens": 0,
                        "output_tokens": 2,
                        "cached_tokens": 0,
                        "cost_decimal": "0.1",
                        "currency": "EUR",
                    }
                ),
            ),
        )
        got = obs_stats.review_stats(self.conn, 20.0).usage
        self.assertEqual((got.input_tokens, got.output_tokens), (0, 2))

    def test_unknown_policy_and_error_text_never_reach_doctor(self) -> None:
        self.reserve()
        self.conn.execute(
            "UPDATE review_control SET policy_json=?",
            (json.dumps({"error": SECRET}),),
        )
        got = self.checks()
        self.assertIs(got["review_policy"]["ok"], False)
        self.assertNotIn(SECRET, json.dumps(got))

    def test_deadline_failure_and_abandoned_recovery_stay_visible(
        self,
    ) -> None:
        attempt = self.reserve()
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, attempt, "deadline", 14.0
            )
        )
        self.assertIs(self.checks()["review_process_deadline"]["ok"], False)
        self.transaction(
            lambda: review_requests.retry_request(
                self.conn,
                self.conn.execute("SELECT id FROM review_request").fetchone()[
                    0
                ],
                15.0,
            )
        )
        request_id = self.conn.execute(
            "SELECT id FROM review_request"
        ).fetchone()[0]
        self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request_id, 1, 16.0
            )
        )
        self.conn.execute("UPDATE review_control SET generation=2")
        self.transaction(
            lambda: review_requests.recover_attempts(self.conn, 2, 17.0)
        )
        self.assertIs(self.checks()["review_abandoned"]["ok"], False)

    def test_mixed_currencies_and_missing_call_usage_remain_unknown(
        self,
    ) -> None:
        self.reserve()
        columns = [
            row[1]
            for row in self.conn.execute("PRAGMA table_info(review_call)")
        ]
        projection = ",".join(
            "round_index+1" if name == "round_index" else name
            for name in columns
        )
        self.conn.execute(
            f"INSERT INTO review_call({','.join(columns)}) "
            f"SELECT {projection} FROM review_call"
        )
        for round_index, currency in ((1, "EUR"), (2, "USD")):
            usage = {
                "input_tokens": 2,
                "output_tokens": None,
                "cached_tokens": 0,
                "cost_decimal": "0.1",
                "currency": currency,
            }
            self.conn.execute(
                "UPDATE review_call SET state='completed',usage_json=? "
                "WHERE round_index=?",
                (json.dumps(usage), round_index),
            )
        got = obs_stats.review_stats(self.conn, 20.0).usage
        self.assertEqual(got.input_tokens, 4)
        self.assertIsNone(got.output_tokens)
        self.assertIsNone(got.currency)
        self.assertIsNone(got.cost_decimal)
        self.conn.execute(
            "UPDATE review_call SET usage_json=NULL WHERE round_index=2"
        )
        self.assertIsNone(
            obs_stats.review_stats(self.conn, 20.0).usage.input_tokens
        )

    def test_windows_diagnostic_is_unqualified_and_preserves_repair(
        self,
    ) -> None:
        from_path = self.home / "review.supervisor.repair.json"
        from_path.write_text(SECRET)
        from_path.chmod(0o600)
        with (
            mock.patch("muninn.obs_status.sys", mock.Mock(platform="win32")),
            mock.patch.object(
                obs, "run", return_value=mock.Mock(returncode=1)
            ),
        ):
            got = obs.doctor(self.home, {"HOME": str(self.home)})
        checks = {c["check"]: c for c in got["checks"]}
        self.assertIs(checks["review_windows_repair"]["ok"], False)
        self.assertIsNone(checks["review_pause"]["ok"])
        self.assertEqual(from_path.read_text(), SECRET)
        self.assertNotIn(SECRET, json.dumps(got))
