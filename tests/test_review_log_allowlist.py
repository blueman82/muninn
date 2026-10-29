"""Keep review lifecycle logs operational and free of governed text."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from muninn import obs_log


class ReviewLogAllowlistTests(unittest.TestCase):
    """Exercise real log writes with safe metadata and hostile values."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def write_record(self, record: dict[str, object]) -> dict[str, object]:
        """Append and decode a real poller log line."""
        self.assertTrue(obs_log.log_poller(self.home, record))
        line: dict[str, object] = json.loads(
            (self.home / "poller.log").read_text().splitlines()[-1]
        )
        line.pop("at", None)
        return line

    def test_ordinary_hook_serializes_only_strict_knowledge_ids(self) -> None:
        first, second = "K_" + "a" * 32, "K_" + "0" * 32
        secret = "sensitive_owner_material"
        record: dict[str, object] = {
            "cmd": "hook start",
            "event": "start",
            "actor": "codex:session",
            "knowledge_ids": [
                first,
                1,
                True,
                False,
                None,
                1.5,
                "K1",
                "R_" + "a" * 32,
                "k_" + "a" * 32,
                "K_" + "A" * 32,
                "K_" + "g" * 32,
                "K_" + "a" * 31,
                "K_" + "a" * 33,
                first + "\n",
                " " + first,
                secret,
                second,
            ],
            "returned_ids": [7, True, first, secret],
            "ids": [9, False, second, secret],
            "scope_id": 4,
            "exit": 0,
            "text": secret,
        }
        self.assertTrue(obs_log.log_call(self.home, record, env={}))
        data = (self.home / "calls.jsonl").read_bytes()
        line: dict[str, object] = json.loads(data)
        self.assertIsInstance(line.pop("at"), (int, float))
        self.assertEqual(
            line,
            {
                "cmd": "hook start",
                "event": "start",
                "actor": "codex:session",
                "knowledge_ids": [first, second],
                "returned_ids": [7],
                "ids": [9],
                "scope_id": 4,
                "exit": 0,
            },
        )
        self.assertNotIn(secret.encode(), data)

    def test_ordinary_hook_knowledge_ids_keep_call_line_bounded(self) -> None:
        knowledge_ids = [f"K_{number:032x}" for number in range(200)]
        record: dict[str, object] = {
            "cmd": "hook prompt",
            "knowledge_ids": knowledge_ids,
        }
        self.assertTrue(obs_log.log_call(self.home, record, env={}))
        data = (self.home / "calls.jsonl").read_bytes()
        self.assertLessEqual(len(data), 1024)
        self.assertEqual(len(data.splitlines()), 1)
        line: dict[str, object] = json.loads(data)
        self.assertEqual(line["cmd"], "hook prompt")
        kept = line["knowledge_ids"]
        self.assertIsInstance(kept, list)
        assert isinstance(kept, list)
        self.assertGreater(len(kept), 0)
        self.assertLess(len(kept), len(knowledge_ids))
        self.assertEqual(kept, knowledge_ids[: len(kept)])

    def test_review_codes_and_ids_allowed(self) -> None:
        record: dict[str, object] = {
            "event": "review_attempt_completed",
            "request_id": "R_" + "1" * 32,
            "attempt_id": "A_" + "2" * 32,
            "knowledge_id": "K_" + "3" * 32,
            "evidence_id": "E_" + "4" * 32,
            "unresolved_id": "U_" + "5" * 32,
            "knowledge_ids": ["K_" + "3" * 32],
            "evidence_ids": ["E_" + "4" * 32],
            "unresolved_ids": ["U_" + "5" * 32],
            "state": "completed",
            "outcome": "completed_unresolved",
            "error_code": None,
            "generation": 1,
            "round_index": 2,
            "revision": 3,
            "eligible_records": 4,
            "completed_records": 0,
            "unresolved_items": 1,
            "observed_enqueue_busy": None,
            "accepted": True,
            "busy_count_complete": False,
            "started_at": 100.0,
            "progress_at": 102.5,
            "ended_at": None,
            "elapsed_seconds": 2.5,
            "deadline_seconds": 10.0,
            "commit_delay_seconds": 0.0,
            "cancel_delay_seconds": None,
            "counts": {"pending": 2, "completed": 0},
            "usage": {
                "input_tokens": None,
                "output_tokens": 0,
                "cached_tokens": 2,
                "cost_decimal": "0.0010",
                "currency": "USD",
            },
        }
        self.assertEqual(self.write_record(record), record)
        for event in (
            "review_enqueue_busy",
            "review_enqueued",
            "review_capture_recovered",
            "review_attempt_started",
            "review_attempt_progress",
            "review_attempt_completed",
            "review_attempt_failed",
            "review_attempt_cancelled",
            "review_attempt_abandoned",
            "review_call_reserved",
            "review_call_launched",
            "review_call_completed",
            "review_call_unknown",
            "review_commit_rejected",
        ):
            with self.subTest(event=event):
                self.assertEqual(
                    self.write_record({"event": event}), {"event": event}
                )
        for code in (
            "rejected_validation",
            "rejected_evidence",
            "rejected_scope",
            "rejected_authority",
            "rejected_revision",
            "rejected_erased",
            "rejected_fenced",
            "auth",
            "config",
            "evidence_byte_limit",
            "evidence_record_limit",
            "evidence_work_limit",
            "evidence_selection_limit",
            "deadline",
            "budget",
            "unavailable",
            "review_policy_required",
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    self.write_record(
                        {
                            "event": "review_commit_rejected",
                            "error_code": code,
                        }
                    ),
                    {"event": "review_commit_rejected", "error_code": code},
                )
        self.assertEqual(
            self.write_record(
                {
                    "event": "review_call_unknown",
                    "usage": None,
                }
            ),
            {"event": "review_call_unknown", "usage": None},
        )

    def test_review_scalar_metadata_keeps_call_line_bounded(self) -> None:
        usage = {
            "input_tokens": None,
            "output_tokens": 0,
            "cached_tokens": 2,
            "cost_decimal": "0.0010",
            "currency": "USD",
        }
        record: dict[str, object] = {
            "event": "review_attempt_completed",
            "request_id": "R_" + "1" * 32,
            "attempt_id": "A_" + "2" * 32,
            "knowledge_id": "K_" + "3" * 32,
            "evidence_id": "E_" + "4" * 32,
            "unresolved_id": "U_" + "5" * 32,
            "state": "completed",
            "outcome": "completed_unresolved",
            "error_code": None,
            "generation": 1,
            "round_index": 2,
            "revision": 3,
            "eligible_records": 4,
            "completed_records": 0,
            "unresolved_items": 1,
            "observed_enqueue_busy": None,
            "accepted": True,
            "busy_count_complete": False,
            "started_at": 100.0,
            "progress_at": 102.5,
            "ended_at": None,
            "elapsed_seconds": 2.5,
            "deadline_seconds": 10.0,
            "commit_delay_seconds": 0.0,
            "cancel_delay_seconds": None,
            "counts": {"pending": 2, "completed": 0},
            "usage": usage,
            "reserved_at": 100.5,
            "launched_at": 100.5,
            "committed_at": 100.5,
            "oldest_pending_age_seconds": 100.5,
            "last_progress_age_seconds": 100.5,
            "schema_version": 1,
            "pid": 123,
            "missed_acknowledgments": 0,
            "recovery_gaps": 0,
        }
        self.assertTrue(obs_log.log_call(self.home, record, env={}))
        data = (self.home / "calls.jsonl").read_bytes()
        self.assertLessEqual(len(data), 1024)
        line: dict[str, object] = json.loads(data)
        for key in (
            "event",
            "request_id",
            "attempt_id",
            "knowledge_id",
            "evidence_id",
            "unresolved_id",
            "state",
            "outcome",
            "error_code",
            "accepted",
            "busy_count_complete",
            "usage",
        ):
            self.assertEqual(line[key], record[key])
        self.assertIsInstance(line["at"], (int, float))

    def test_oversized_usage_never_appended(self) -> None:
        record: dict[str, object] = {
            "event": "review_call_completed",
            "usage": {"input_tokens": 10**1100, "output_tokens": None},
        }
        self.assertFalse(obs_log.log_call(self.home, record, env={}))
        self.assertFalse((self.home / "calls.jsonl").exists())

    def test_content_and_validation_text_refused(self) -> None:
        secret = "sensitive_owner_material"
        fields = (
            "transcript",
            "knowledge",
            "prompt",
            "result",
            "credential",
            "validation_error",
            "detail",
            "exc",
            "cmd",
            "actor",
            "error",
            "requested",
            "observed",
            "no_change_reason",
            "budget_account_id",
        )
        record: dict[str, object] = dict.fromkeys(fields, secret)
        record["event"] = "review_enqueue_busy"
        self.assertEqual(
            self.write_record(record), {"event": "review_enqueue_busy"}
        )
        invalid: dict[str, object] = {
            "request_id": "K_" + "1" * 32,
            "attempt_id": 12,
            "knowledge_id": "K12",
            "evidence_id": "E_" + "A" * 32,
            "unresolved_id": "U_" + "1" * 31,
            "error_code": secret,
            "outcome": secret,
            "state": secret,
            "generation": True,
            "revision": 0,
            "round_index": -1,
            "eligible_records": -1,
            "completed_records": 1.5,
            "elapsed_seconds": float("nan"),
            "deadline_seconds": float("inf"),
            "started_at": -1.0,
            "accepted": "true",
        }
        for key, value in invalid.items():
            with self.subTest(key=key):
                self.assertEqual(
                    self.write_record(
                        {
                            "event": "review_enqueue_busy",
                            key: value,
                        }
                    ),
                    {"event": "review_enqueue_busy"},
                )
        self.assert_usage_filtered(secret)

    def assert_usage_filtered(self, secret: str) -> None:
        """Reject hostile usage, identity lists and unknown event fallbacks."""
        for value in (secret, True, -1, 1.5):
            with self.subTest(usage=value):
                self.assertEqual(
                    self.write_record(
                        {
                            "event": "review_call_completed",
                            "usage": {"input_tokens": value, "prompt": secret},
                        }
                    ),
                    {"event": "review_call_completed", "usage": {}},
                )
        self.assertEqual(
            self.write_record(
                {
                    "event": "review_call_completed",
                    "usage": {
                        "input_tokens": None,
                        "output_tokens": None,
                        "cached_tokens": None,
                        "cost_decimal": None,
                        "currency": None,
                    },
                }
            ),
            {
                "event": "review_call_completed",
                "usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "cached_tokens": None,
                    "cost_decimal": None,
                    "currency": None,
                },
            },
        )
        self.assertEqual(
            self.write_record(
                {
                    "event": "review_enqueue_busy",
                    "knowledge_ids": [
                        1,
                        secret,
                        "R_" + "1" * 32,
                        "K_" + "2" * 32,
                    ],
                    "counts": {secret: 1, "pending": -1, "completed": 0},
                    "usage": {"cost_decimal": "-1", "currency": secret},
                }
            ),
            {
                "event": "review_enqueue_busy",
                "knowledge_ids": [
                    "K_" + "2" * 32,
                ],
                "counts": {"completed": 0},
                "usage": {},
            },
        )
        self.assertEqual(
            self.write_record(
                {
                    "event": "review_unknown",
                    "detail": secret,
                    "exc": secret,
                    "error_code": "rejected_validation",
                    "request_id": "R_" + "1" * 32,
                }
            ),
            {},
        )
        self.assertNotIn(secret, (self.home / "poller.log").read_text())
