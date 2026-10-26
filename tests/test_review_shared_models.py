"""Check shared evidence, policy and lifecycle model constraints."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from muninn.knowledge_models import (
    Expiry,
    KnowledgeContext,
    KnowledgeView,
    Scope,
    SupportedClaim,
)
from muninn.review_evidence_models import (
    EvidenceRecord,
    EvidenceSelector,
    EvidenceWindow,
    InitialEvidence,
    JsonlOriginal,
)
from muninn.review_models import (
    ObservedConfig,
    PilotBudget,
    ProcessIdentity,
    Receipt,
    ReviewContext,
    ReviewPolicy,
    ReviewStats,
    Usage,
)
from tests.test_review_models import (
    ATTEMPT,
    DIGEST,
    EVIDENCE,
    KNOWLEDGE,
    REQUEST,
    fixture,
)


def policy_data() -> dict[str, Any]:
    """Supply explicit policy values with no operational defaults."""
    return {
        "schema_version": 1,
        "policy_id": "test-policy",
        "prompt_digest": DIGEST,
        "projection_version": "test-v1",
        "result_schema_digest": DIGEST,
        "runner": {
            "runner": "claude_cli",
            "executable": "claude",
            "version": "test",
            "model": "explicit-model",
            "reasoning": "explicit-reasoning",
        },
        "max_rounds": 2,
        "max_input_bytes": 1000,
        "max_output_bytes": 1000,
        "context_bytes": 1000,
        "deadline_seconds": 60.0,
        "max_attempts": 2,
        "retry_backoff_seconds": [1.0],
        "concurrency": 1,
        "hook_wait_seconds": 0.0,
        "allow_global": False,
        "budget": {
            "account_id": "test-account",
            "max_model_calls": 2,
            "max_wall_seconds": 60.0,
            "max_cost_decimal": None,
            "conservative_call_cost_decimal": None,
            "currency": None,
            "approved_by": "test-owner",
            "approved_at": 0.0,
        },
    }


class ReviewSharedModelTests(unittest.TestCase):
    """Exercise constraints shared by later data-only consumers."""

    def test_support_scope_expiry_and_frozen_models(self) -> None:
        for refs in ([], [EVIDENCE, EVIDENCE], [KNOWLEDGE]):
            with self.assertRaises(ValidationError):
                SupportedClaim.model_validate(
                    {"text": "x", "evidence_refs": refs}
                )
        for data in (
            {"project_id": None, "task": "x"},
            {"project_id": "x"},
            {"project_id": " ", "task": None},
        ):
            with self.assertRaises(ValidationError):
                Scope.model_validate(data)
        self.assertIsNone(Scope(project_id=None, task=None).project_id)
        for at, condition, refs in (
            (None, None, [EVIDENCE]),
            (0.0, None, []),
            (-1.0, None, [EVIDENCE]),
            (float("nan"), None, [EVIDENCE]),
        ):
            with self.assertRaises(ValidationError):
                Expiry.model_validate(
                    {"at": at, "condition": condition, "evidence_refs": refs}
                )
        scope = Scope(project_id="x", task=None)
        with self.assertRaises(ValidationError):
            scope.task = "changed"

    def test_evidence_coordinates_and_selectors(self) -> None:
        window = {
            "source": {"provider": "codex", "thread_id": "thread:part"},
            "from_line": 1,
            "through_line": 2,
            "boundary_hash": DIGEST,
        }
        self.assertEqual(
            EvidenceWindow.model_validate(window).source.thread_id,
            "thread:part",
        )
        for field, value in (
            ("from_line", 0),
            ("from_line", True),
            ("through_line", 0),
            ("boundary_hash", DIGEST.upper()),
        ):
            with self.assertRaises(ValidationError):
                EvidenceWindow.model_validate({**window, field: value})
        for start, end in ((0, None), (None, 1), (-1, 1), (2, 1), (True, 2)):
            with self.assertRaises(ValidationError):
                EvidenceSelector(json_pointer="/content", start=start, end=end)
        self.assertEqual(
            EvidenceSelector(json_pointer="/content", start=1, end=2).start, 1
        )
        with self.assertRaises(ValidationError):
            JsonlOriginal(
                kind="jsonl", path=Path("test"), byte_offset=0, byte_length=0
            )
        with self.assertRaises(ValidationError):
            InitialEvidence(
                excerpts=(), outcomes=(), truncated=False, bytes_used=-1
            )

    def test_policy_usage_and_shared_context_constraints(self) -> None:
        data = policy_data()
        policy = ReviewPolicy.model_validate(data)
        for field, bad in (
            ("max_rounds", 0),
            ("concurrency", True),
            ("deadline_seconds", float("inf")),
            ("hook_wait_seconds", -1.0),
            ("retry_backoff_seconds", []),
        ):
            with self.assertRaises(ValidationError):
                ReviewPolicy.model_validate({**data, field: bad})
        with self.assertRaises(ValidationError):
            ReviewPolicy.model_validate({})
        for cost in ("-1", "NaN", "Infinity", "not-money"):
            with self.assertRaises(ValidationError):
                Usage(
                    input_tokens=None,
                    output_tokens=None,
                    cached_tokens=None,
                    cost_decimal=cost,
                    currency="USD",
                )
        unknown = Usage(
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            cost_decimal=None,
            currency=None,
        )
        self.assertIsNone(unknown.input_tokens)
        with self.assertRaises(ValidationError):
            Usage(
                input_tokens=True,
                output_tokens=None,
                cached_tokens=None,
                cost_decimal=None,
                currency=None,
            )
        with self.assertRaises(ValidationError):
            PilotBudget.model_validate(
                {**data["budget"], "max_model_calls": 0}
            )
        context = ReviewContext(
            request_id=REQUEST,
            attempt_id=ATTEMPT,
            generation=1,
            scope=Scope(project_id="x", task=None),
            policy=policy,
            admitted_windows=(),
            evidence_ids=(),
            round_index=1,
            deadline=1.0,
        )
        self.assertEqual(context.round_index, 1)
        with self.assertRaises(ValidationError):
            ProcessIdentity(
                pid=0, created="native", pgid=None, job_marker=None
            )
        self.assertIsNone(
            ObservedConfig(
                runner=None, version=None, model=None, reasoning=None
            ).model
        )

    def test_erased_views_and_note_context(self) -> None:
        data = {
            "schema_version": 1,
            "id": KNOWLEDGE,
            "revision": 1,
            "state": "active",
            "body": fixture("note")["changes"][0],
            "origin": "review",
            "expiry": None,
            "created_at": 0.0,
            "retracted_at": None,
            "retracted_reason": None,
        }
        del data["body"]["action"]
        view = KnowledgeView.model_validate(data)
        context = KnowledgeContext(
            schema_version=1,
            notes=[view],
            guides=[],
            unresolved_ids=[],
            truncated=False,
        )
        self.assertEqual(context.notes[0].id, KNOWLEDGE)
        for state, body, reason in (
            ("erased", data["body"], None),
            ("erased", None, "private"),
        ):
            with self.assertRaises(ValidationError):
                KnowledgeView.model_validate(
                    {
                        **data,
                        "state": state,
                        "body": body,
                        "retracted_reason": reason,
                    }
                )
        guide = fixture("guide")["changes"][0]
        del guide["action"]
        with self.assertRaises(ValidationError):
            KnowledgeContext.model_validate(
                {
                    "schema_version": 1,
                    "notes": [{**data, "body": guide}],
                    "guides": [],
                    "unresolved_ids": [],
                    "truncated": False,
                }
            )

    def test_record_provenance_and_erased_excerpts(self) -> None:
        record = {
            "id": EVIDENCE,
            "provider": "codex",
            "thread_id": "thread",
            "session_root": "root",
            "line": 1,
            "part": 1,
            "line_sha256": DIGEST,
            "source_version": "v1",
            "projection_version": "v1",
            "original_locator": {
                "kind": "jsonl",
                "path": "fixture.jsonl",
                "byte_offset": 0,
                "byte_length": 1,
            },
            "selector": {
                "json_pointer": "/content",
                "start": None,
                "end": None,
            },
            "selector_digest": DIGEST,
            "evidence_type": "owner_statement",
            "authority": "direct_owner",
            "scope": {
                "project_id": "x",
                "task": None,
            },
            "excerpt": "source",
            "state": "live",
            "selected_at": 0.0,
        }
        self.assertEqual(
            EvidenceRecord.model_validate_json(
                json.dumps(record), strict=True
            ).original_locator.kind,
            "jsonl",
        )
        for field, value in (
            ("id", KNOWLEDGE),
            ("part", 0),
            ("selected_at", -1.0),
            ("provider", "unknown"),
            ("state", "erased"),
            ("authority", "owner"),
        ):
            with self.assertRaises(ValidationError):
                EvidenceRecord.model_validate_json(
                    json.dumps({**record, field: value}), strict=True
                )
        erased = {
            **record,
            "state": "erased",
            "excerpt": None,
            "original_locator": {
                "kind": "cursor_db",
                "database": "db",
                "conversation_id": "thread",
                "record_key": "row",
            },
        }
        self.assertIsNone(
            EvidenceRecord.model_validate_json(
                json.dumps(erased), strict=True
            ).excerpt
        )
        for pointer in ("content", "/bad~2escape"):
            with self.assertRaises(ValidationError):
                EvidenceSelector(json_pointer=pointer, start=None, end=None)

    def test_receipts_stats_and_budget_nullable_values(self) -> None:
        receipt = {
            "schema_version": 1,
            "request_id": REQUEST,
            "attempt_id": ATTEMPT,
            "applied": [],
            "unresolved_ids": [],
            "committed_at": 0.0,
            "outcome": "no_change",
            "no_change_reason": "No change.",
        }
        self.assertEqual(Receipt.model_validate(receipt).outcome, "no_change")
        self.assertIsNone(
            Receipt.model_validate(
                {**receipt, "no_change_reason": None}
            ).no_change_reason
        )
        for field, value in (
            ("request_id", ATTEMPT),
            ("committed_at", float("inf")),
            ("outcome", "completed"),
        ):
            with self.assertRaises(ValidationError):
                Receipt.model_validate({**receipt, field: value})
        usage = {
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "cost_decimal": None,
            "currency": None,
        }
        stats = {
            "schema_version": 1,
            "admission": {"enabled": False, "policy_status": "policy_absent"},
            "requests_by_state": {"pending": 1},
            "calls_by_state": {"unknown": 1},
            "oldest_pending_age_seconds": None,
            "last_progress_age_seconds": None,
            "eligible_records": 0,
            "completed_records": 0,
            "unresolved_items": 0,
            "usage": usage,
            "observed_enqueue_busy": None,
            "busy_count_complete": False,
        }
        self.assertIsNone(
            ReviewStats.model_validate(stats).observed_enqueue_busy
        )
        for field, value in (
            ("requests_by_state", {"invented": 1}),
            ("calls_by_state", {"unknown": -1}),
            ("eligible_records", True),
            ("oldest_pending_age_seconds", -1.0),
        ):
            with self.assertRaises(ValidationError):
                ReviewStats.model_validate({**stats, field: value})
        budget = policy_data()["budget"]
        for field, value in (
            ("max_wall_seconds", 0.0),
            ("approved_at", -1.0),
            ("max_cost_decimal", "-1"),
            ("max_cost_decimal", "2"),
        ):
            with self.assertRaises(ValidationError):
                PilotBudget.model_validate({**budget, field: value})
        self.assertEqual(
            PilotBudget.model_validate(
                {**budget, "max_cost_decimal": "2.50", "currency": "USD"}
            ).max_cost_decimal,
            "2.50",
        )
