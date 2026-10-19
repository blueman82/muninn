"""Check strict native-envelope decoding without executing a provider."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from muninn import review_output
from tests import test_review_budget as budget_tests


class ReviewOutputTests(unittest.TestCase):
    """Keep native output, observations and actual charges distinct."""

    def envelope(self, **changes: object) -> bytes:
        """Encode a documented-shaped local fake success envelope."""
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": {"op": "open", "knowledge_id": "fake"},
                **changes,
            }
        ).encode()

    def test_success_metadata_and_estimates_remain_separate(self) -> None:
        raw = self.envelope(
            modelUsage={"reported-model": {"costUSD": 0.01}},
            usage={
                "input_tokens": 12,
                "output_tokens": 3,
                "cache_read_input_tokens": 4,
                "cache_creation_input_tokens": 9,
            },
            total_cost_usd=0.01,
            permission_denials=[],
            deferred_tool_use=None,
        )
        body, observed, usage = review_output.decode_claude_output(
            raw, len(raw)
        )
        self.assertEqual(body, b'{"knowledge_id":"fake","op":"open"}')
        self.assertEqual(observed.model, "reported-model")
        self.assertIsNone(observed.runner)
        self.assertIsNone(observed.version)
        self.assertIsNone(observed.reasoning)
        assert usage is not None
        self.assertEqual(
            (usage.input_tokens, usage.output_tokens, usage.cached_tokens),
            (12, 3, 4),
        )
        self.assertIsNone(usage.cost_decimal)
        self.assertIsNone(usage.currency)

    def test_unknown_usage_and_models_are_not_inferred(self) -> None:
        for model in (None, {}, {"a": {}, "b": {}}):
            _, observed, usage = review_output.decode_claude_output(
                self.envelope(modelUsage=model), 1000
            )
            self.assertIsNone(observed.model)
            self.assertIsNone(usage)
        _, _, usage = review_output.decode_claude_output(
            self.envelope(
                usage={"input_tokens": None, "cache_creation_input_tokens": 9}
            ),
            1000,
        )
        assert usage is not None
        self.assertTrue(
            all(value is None for value in usage.model_dump().values())
        )

    def test_error_permission_deferred_and_invalid_shapes_refuse(self) -> None:
        cases = [
            {"type": "assistant"},
            {"subtype": "error_max_turns"},
            {"is_error": True},
            {"is_error": 0},
            {"structured_output": []},
            {"permission_denials": [{}]},
            {"permission_denials": False},
            {"permission_denials": {}},
            {"deferred_tool_use": {}},
            {"deferred_tool_use": False},
            {"deferred_tool_use": []},
            {"usage": []},
            {"modelUsage": []},
            {"modelUsage": {"": {}}},
            {"modelUsage": {"model": False}},
        ]
        for changes in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "^invalid_native_output$"),
            ):
                review_output.decode_claude_output(
                    self.envelope(**changes), 1000
                )
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
        ):
            for value in (-1, True, 1.5, "2"):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    review_output.decode_claude_output(
                        self.envelope(usage={field: value}), 1000
                    )

    def test_json_and_whole_envelope_bounds_refuse_finitely(self) -> None:
        for raw in (
            b"{}{}",
            b"```json\n{}\n```",
            b"\xff",
            b'{"x":1,"x":2}',
            b'{"x":NaN}',
            b'{"x":1e9999}',
            b"1" * 5000,
            b"[" * 5000 + b"0" + b"]" * 5000,
        ):
            with (
                self.subTest(size=len(raw)),
                self.assertRaisesRegex(ValueError, "^invalid_native_output$"),
            ):
                review_output.decode_claude_output(raw, 20000)
        raw = self.envelope(extra="large ignored envelope")
        for limit in (True, 0, -1, len(raw) - 1):
            with (
                self.subTest(limit=limit),
                self.assertRaisesRegex(ValueError, "^native_output_limit$"),
            ):
                review_output.decode_claude_output(raw, limit)

    def test_import_graph_is_data_only(self) -> None:
        code = (
            "import sys; from muninn import review_output; "
            "assert not any(n in sys.modules for n in "
            "('sqlite3','muninn.review_runner','muninn.review_rounds','muninn.store'))"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_canonical_body_size_is_checked_again(self) -> None:
        raw = (
            b'{"type":"result","subtype":"success","is_error":false,'
            b'"structured_output":{"n":[' + b",".join([b"1e20"] * 300) + b"]}}"
        )
        with self.assertRaisesRegex(ValueError, "^native_output_limit$"):
            review_output.decode_claude_output(raw, len(raw))

    def test_estimate_does_not_release_conservative_reservation(self) -> None:
        case = budget_tests.ReviewBudgetTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.cost_policy("decoder", "0.75", "0.50")
        context = case.context()
        case.transaction(
            lambda: budget_tests.review_budget.reserve_call(
                case.conn, context, 13.0
            )
        )
        _, observed, usage = review_output.decode_claude_output(
            self.envelope(total_cost_usd=0.01, usage={"input_tokens": 1}), 1000
        )
        assert usage is not None
        case.conn.execute(
            "UPDATE review_call SET state='completed',"
            "observed_json=?,usage_json=?",
            (observed.model_dump_json(), usage.model_dump_json()),
        )
        with self.assertRaisesRegex(ValueError, "budget_cost_limit"):
            case.transaction(
                lambda: budget_tests.review_budget.reserve_call(
                    case.conn,
                    context.model_copy(update={"round_index": 2}),
                    14.0,
                )
            )
        self.assertEqual(
            case.conn.execute(
                "SELECT cost_reservation_decimal FROM review_call"
            ).fetchone()[0],
            "0.50",
        )
