"""Keep correctable model schema errors separate from terminal refusals."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from muninn import review_correction, review_rounds
from tests.test_review_rounds import context


class ReviewCorrectionTests(unittest.TestCase):
    """Exercise format eligibility only after strict decoding succeeds."""

    def setUp(self) -> None:
        self.context = context()
        timer = mock.patch(
            "muninn.review_rounds.time.monotonic", return_value=10.0
        )
        timer.start()
        self.addCleanup(timer.stop)

    def test_only_schema_failures_are_correctable(self) -> None:
        valid = {
            "op": "open",
            "knowledge_id": "K_" + "3" * 32,
            "revision": None,
        }
        raw = json.dumps(valid).encode()
        cases = [
            ("unknown_op", b'{"op":"shell","command":"private marker"}', True),
            ("missing_field", b'{"op":"open"}', True),
            (
                "wrong_type",
                json.dumps({**valid, "revision": True}).encode(),
                True,
            ),
            ("extra_field", json.dumps({**valid, "extra": 1}).encode(), True),
            ("duplicate_keys", b'{"op":"shell","op":"open"}', False),
            ("nan", b'{"op":"shell","value":NaN}', False),
            ("infinity", b'{"op":"shell","value":Infinity}', False),
            ("overflow", b'{"op":"shell","value":1e999}', False),
            ("surrogate", b'{"op":"shell","value":"\\ud800"}', False),
            ("utf8", b'{"op":"shell","value":"\xff"}', False),
            ("json", b'{"op":"shell",', False),
            (
                "oversize",
                raw + b" " * self.context.policy.max_output_bytes,
                False,
            ),
            ("deadline", raw, False),
            ("round_limit", raw, False),
            ("invalid_context", raw, False),
            (
                "scope",
                json.dumps(
                    {
                        "op": "search",
                        "query": "x",
                        "scope": {"project_id": "other", "task": None},
                    }
                ).encode(),
                False,
            ),
        ]
        for name, value, correctable in cases:
            updates: dict[str, object] = {}
            if name == "deadline":
                updates["deadline"] = 10.0
            elif name == "round_limit":
                updates["round_index"] = self.context.policy.max_rounds + 1
            elif name == "invalid_context":
                updates["generation"] = 0
            admitted = self.context.model_copy(update=updates)
            with self.subTest(case=name):
                with self.assertRaises(ValueError) as error:
                    review_rounds.parse_round(value, admitted)
                if correctable:
                    self.assertIsInstance(
                        error.exception, review_rounds.RoundFormatError
                    )
                else:
                    self.assertNotIsInstance(
                        error.exception, review_rounds.RoundFormatError
                    )
                if correctable:
                    self.assertEqual(str(error.exception), "model_format")
                    self.assertEqual(
                        getattr(error.exception, "code", None), "model_format"
                    )

    def test_unknown_operation_never_reaches_trusted_dispatch(self) -> None:
        with (
            mock.patch(
                "muninn.review_rounds._scope",
                side_effect=AssertionError("unvalidated_dispatch"),
            ),
            self.assertRaises(review_rounds.RoundFormatError),
        ):
            review_rounds.parse_round(b'{"op":"shell"}', self.context)

    def test_parsing_never_verifies_original_evidence(self) -> None:
        raw = json.dumps(
            {
                "op": "evidence",
                "locator": {
                    "source": {"provider": "claude", "thread_id": "synthetic"},
                    "line": 3,
                    "part": 1,
                    "line_sha256": "b" * 64,
                },
                "selector": {
                    "json_pointer": "/message/content",
                    "start": None,
                    "end": None,
                },
            }
        ).encode()
        with (
            mock.patch(
                "muninn.review_rounds.check_window",
                side_effect=AssertionError("original_read_during_parse"),
            ),
            mock.patch(
                "muninn.review_rounds.validate_evidence",
                side_effect=AssertionError("original_read_during_parse"),
            ),
            mock.patch(
                "muninn.review_rounds.store.connect_rw",
                side_effect=AssertionError("database_open_during_parse"),
            ),
        ):
            request = review_rounds.parse_round(raw, self.context)
        assert isinstance(request, review_rounds.EvidenceRequest)
        self.assertEqual(request.locator.line, 3)

    def test_feedback_accepts_only_finite_structured_fields(self) -> None:
        accepted = [
            {
                "kind": "format",
                "code": "model_format",
                "collection": None,
                "index": None,
            },
            *[
                {
                    "kind": "reduction",
                    "code": code,
                    "collection": collection,
                    "index": index,
                }
                for code in (
                    "candidate_scope",
                    "owner_support",
                    "duplicate_target",
                    "target_overlap",
                )
                for collection in ("changes", "unresolved")
                for index in (0, 1)
            ],
        ]
        for data in accepted:
            with self.subTest(data=data):
                feedback = review_correction.CorrectionFeedback.model_validate(
                    data
                )
                self.assertEqual(feedback.model_dump(mode="json"), data)

    def test_feedback_rejects_mismatched_or_unbounded_fields(self) -> None:
        valid = {
            "kind": "reduction",
            "code": "owner_support",
            "collection": "changes",
            "index": 0,
        }
        format_data = {
            "kind": "format",
            "code": "model_format",
            "collection": None,
            "index": None,
        }
        cases = [
            {**format_data, "collection": "changes"},
            {**format_data, "index": 0},
            {**format_data, "collection": "unresolved", "index": 0},
            {**valid, "kind": "format"},
            {**valid, "code": "model_format"},
            {**valid, "collection": None},
            {**valid, "index": None},
            {**valid, "collection": None, "index": None},
            {**valid, "index": -1},
            {**valid, "index": True},
            {**valid, "index": 1.0},
            {**valid, "index": "0"},
            {**valid, "code": "private error marker"},
            {**valid, "collection": "private collection marker"},
            {**valid, "kind": "private kind marker"},
            {**valid, "error": "private model text"},
            {
                "kind": "format",
                "code": "owner_support",
                "collection": None,
                "index": None,
            },
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                review_correction.CorrectionFeedback.model_validate(data)
