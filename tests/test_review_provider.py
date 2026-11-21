"""Check provider argv and decoding without any model call."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from unittest import mock

from muninn import review_provider_claude as claude
from muninn import review_provider_codex as codex
from muninn.review_settings import Settings

SETTINGS = Settings(model="sonnet", effort="low")


def _envelope(**over: object) -> bytes:
    body: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {"b": 1, "a": 2},
    }
    return json.dumps(body | over).encode()


class ClaudeTests(unittest.TestCase):
    """Claude argv and envelope decoding."""

    def test_argv_is_exact(self) -> None:
        got = claude.argv(SETTINGS, "{}", "/x/claude", Path("/w"))
        self.assertEqual(
            got[:6],
            (
                "/x/claude",
                "-p",
                "--json-schema",
                "{}",
                "--output-format",
                "json",
            ),
        )
        self.assertIn(("--tools", ""), list(pairwise(got)))
        self.assertIn(("--setting-sources", ""), list(pairwise(got)))
        self.assertIn("--no-session-persistence", got)
        self.assertEqual(got[-4:], ("--model", "sonnet", "--effort", "low"))

    def test_decode_success_is_canonical(self) -> None:
        self.assertEqual(claude.decode(_envelope(), 100), b'{"a":2,"b":1}')

    def test_decode_rejects_bad_envelopes(self) -> None:
        for raw in (
            _envelope(is_error=True),
            _envelope(subtype="error"),
            _envelope(structured_output="text"),
            b"[]",
            b"not json",
        ):
            with self.assertRaises(ValueError) as ctx:
                claude.decode(raw, 1000)
            self.assertEqual(str(ctx.exception), "invalid_native_output")

    def test_decode_limit(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            claude.decode(_envelope(), 10)
        self.assertEqual(str(ctx.exception), "output_limit")

    def test_find_executable_falls_back_to_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / ".local" / "bin" / "claude"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\n")
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {"HOME": tmp, "PATH": ""}):
                self.assertEqual(claude.find_executable(), str(exe))


class CodexTests(unittest.TestCase):
    """Codex argv and JSONL decoding."""

    def test_argv_is_exact(self) -> None:
        got = codex.argv(SETTINGS, "{}", "codex", Path("/w"))
        self.assertEqual(
            got,
            (
                "codex",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-C",
                "/w",
                "--output-schema",
                "/w/schema.json",
                "--json",
                "--model",
                "sonnet",
                "-c",
                "model_reasoning_effort=low",
                "-",
            ),
        )

    def test_decode_final_message(self) -> None:
        lines = [
            {"type": "thread.started", "thread_id": "t"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"b":1,"a":2}'},
            },
        ]
        raw = "\n".join(json.dumps(x) for x in lines).encode()
        self.assertEqual(codex.decode(raw, 1000), b'{"a":2,"b":1}')

    def test_decode_rejects_tool_use_and_missing_message(self) -> None:
        tool = json.dumps(
            {"type": "item.completed", "item": {"type": "command_execution"}}
        ).encode()
        for raw in (tool, b'{"type":"turn.started"}'):
            with self.assertRaises(ValueError):
                codex.decode(raw, 1000)


if __name__ == "__main__":
    unittest.main()
