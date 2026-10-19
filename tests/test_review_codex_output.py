"""Exercise data-only native JSONL decoding and nullable observations."""

from __future__ import annotations

import importlib
import importlib.util
import json
import unittest
from typing import Any


def stream(body: str = '{"answer":"ok"}', usage: object = None) -> bytes:
    """Produce the four bounded event shapes observed by the canary."""
    return b"".join(
        json.dumps(event).encode() + b"\n"
        for event in (
            {"type": "thread.started", "thread_id": "thread"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "agent_message",
                    "text": json.dumps({"payload_json": body}),
                },
            },
            {"type": "turn.completed", "usage": usage},
        )
    )


class CodexOutputTests(unittest.TestCase):
    """Reject native actions, ambiguous terminals and malformed data."""

    def decoder(self) -> Any:
        """Require the native decoder without masking a missing module."""
        self.assertIsNotNone(
            importlib.util.find_spec("muninn.review_codex_output")
        )
        return importlib.import_module(
            "muninn.review_codex_output"
        ).decode_codex_output

    def test_success_preserves_unknown_observations(self) -> None:
        body, observed, usage = self.decoder()(stream(), 4096)
        self.assertEqual(json.loads(body), {"answer": "ok"})
        self.assertTrue(all(v is None for v in observed.model_dump().values()))
        self.assertIsNone(usage)
        _, _, counters = self.decoder()(
            stream(
                usage={
                    "input_tokens": 9,
                    "cached_input_tokens": 2,
                    "output_tokens": 3,
                    "reasoning_output_tokens": 1,
                    "cache_write_input_tokens": 0,
                }
            ),
            4096,
        )
        self.assertEqual(counters.input_tokens, 9)
        self.assertEqual(counters.cached_tokens, 2)
        self.assertIsNone(counters.cost_decimal)
        self.assertIsNone(counters.currency)

    def test_malformed_and_ambiguous_streams_refused(self) -> None:
        decode = self.decoder()
        raw = stream()
        events = raw.splitlines(keepends=True)
        invalid = [
            raw[:-10],
            raw + events[-1],
            raw + b"{}\n",
            b"\n" + raw,
            b"".join(events[:3] + events[2:]),
            raw.replace(
                b'"type": "turn.started"',
                b'"type":"turn.started","type":"turn.started"',
            ),
            stream('{"answer":"a","answer":"b"}'),
            stream('{"answer":NaN}'),
            stream("[]"),
            stream(usage={"input_tokens": True}),
            stream(usage={"input_tokens": -1}),
            raw.replace(b'"turn.completed"', b'"turn.failed"'),
            raw.replace(b'"agent_message"', b'"refusal"'),
            raw.replace(b'"turn.started"', b'"unknown.event"'),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode(value, 4096)
        with self.assertRaisesRegex(ValueError, "native_output_limit"):
            decode(raw, len(raw) - 1)

    def test_tool_event_is_distinct_posthoc_failure(self) -> None:
        decode = self.decoder()
        for kind in (
            "command_execution",
            "mcp_tool_call",
            "web_search",
            "file_change",
        ):
            raw = stream().replace(
                b'"agent_message"', json.dumps(kind).encode()
            )
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(ValueError, "native_tool_event"),
            ):
                decode(raw, 4096)

    def test_outer_envelope_is_exact_and_inner_is_strict(self) -> None:
        for outer in (
            "{}",
            '{"payload_json":42}',
            '{"payload_json":"{}","extra":1}',
            '{"payload_json":"{}","payload_json":"{}"}',
            '{"answer":"old wire"}',
        ):
            events = [json.loads(line) for line in stream().splitlines()]
            events[2]["item"]["text"] = outer
            raw = b"\n".join(json.dumps(e).encode() for e in events)
            with self.subTest(outer=outer), self.assertRaises(ValueError):
                self.decoder()(raw, 4096)
        for inner in ("{} {}", '{"a":Infinity}', '{"a":1,"a":2}', "null"):
            with self.subTest(inner=inner), self.assertRaises(ValueError):
                self.decoder()(stream(inner), 4096)
