"""Claude transcript classification tests."""

from __future__ import annotations

import unittest

from pctx import classify as c
from tests.classify_claude_support import (
    MAIN,
    SESSION,
    SUB,
    cev,
    claude_rec,
    text_block,
    tool_use,
)


class ClaudeTests(unittest.TestCase):
    """Claude thread identity and event extraction."""

    def test_claude_subagents_path_and_sidechain_are_subagent(self) -> None:
        first_main = {"type": "queue-operation", "sessionId": SESSION}
        self.assertEqual(
            c.claude_thread(MAIN, first_main),
            c.ThreadInfo(
                "claude",
                SESSION,
                SESSION,
                None,
                None,
                "primary",
                "path:main",
                "none",
                None,
            ),
        )
        first_sub = claude_rec("user", "task", sidechain=True)
        self.assertEqual(
            c.claude_thread(SUB, first_sub),
            c.ThreadInfo(
                "claude",
                "agent-a1",
                SESSION,
                SESSION,
                None,
                "subagent",
                "path:subagents",
                "none",
                None,
            ),
        )
        side = c.claude_thread("-work-repo/agent-b2.jsonl", first_sub)
        self.assertEqual(
            (side.thread_class, side.class_reason, side.thread_id),
            ("subagent", "isSidechain", "agent-b2"),
        )
        bare = c.claude_thread(MAIN, {"type": "mode"})
        self.assertEqual((bare.thread_id, bare.session_root), (SESSION,) * 2)
        no_sid = c.claude_thread(SUB, {"type": "user"})  # from the dir
        self.assertEqual(
            (no_sid.session_root, no_sid.parent_thread_id), (SESSION, SESSION)
        )
        short = c.claude_thread("subagents/agent-x.jsonl", {})
        self.assertEqual(short.thread_class, "subagent")

    def test_claude_prompt_reply_and_delegation(self) -> None:
        cases = (
            (claude_rec("user", "hello there"), cev("prompt", "hello there")),
            (
                claude_rec("user", [text_block("a"), text_block("b")]),
                cev("prompt", "a\nb"),
            ),
            (
                claude_rec("user", "the task", sidechain=True),
                cev("delegation", "the task"),
            ),
            (
                claude_rec("assistant", [text_block("done")], sidechain=True),
                cev("reply", "done", role="assistant"),
            ),
        )
        for record, want in cases:
            with self.subTest(want=want.text):
                self.assertEqual(c.claude_events(record, 7), [want])

    def test_claude_thinking_not_stored(self) -> None:
        thinking = {"type": "thinking", "thinking": "hmm", "signature": "s"}
        record = claude_rec("assistant", [thinking, text_block("answer")])
        self.assertEqual(
            c.claude_events(record, 7),
            [cev("reply", "answer", role="assistant")],
        )
        only = claude_rec("assistant", [thinking])
        self.assertEqual(c.claude_events(only, 7), [])

    def test_claude_tool_result_not_stored(self) -> None:
        result = {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "all fine",
            "is_error": False,
        }
        stdout = {"stdout": "all fine", "stderr": "", "interrupted": False}
        cases = (
            claude_rec("user", [result], toolUseResult=stdout),
            claude_rec("user", [result, text_block("[Request interrupted")]),
            claude_rec("user", "Error: x", toolUseResult="Error: x"),
        )
        for record in cases:
            with self.subTest(record=str(record["message"])[:30]):
                self.assertEqual(c.claude_events(record, 7), [])

    def test_claude_ismeta_and_command_wrappers_are_harness(self) -> None:
        prefixes = (
            "<command-name>",
            "<command-message>",
            "<local-command-stdout>",
            "<local-command-caveat>",
            "<system-reminder>",
            "[Request interrupted",
            "Caveat:",
            "Base directory for this skill",
            "<task-notification>",
            "This session is being continued",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                record = claude_rec("user", prefix + " x")
                self.assertEqual(
                    c.claude_events(record, 7),
                    [cev("harness", prefix + " x", tag=prefix)],
                )
        meta = claude_rec("user", "expanded skill", isMeta=True)
        summary = claude_rec("user", "summary", isCompactSummary=True)
        both = claude_rec("user", "Caveat: x", isMeta=True)
        self.assertEqual(c.claude_events(meta, 7)[0].tag, "isMeta")
        self.assertEqual(
            c.claude_events(summary, 7)[0].tag, "isCompactSummary"
        )
        self.assertEqual(c.claude_events(both, 7)[0].tag, "Caveat:")
        plain = claude_rec("user", "why does <system-reminder> appear?")
        self.assertEqual(c.claude_events(plain, 7)[0].kind, "prompt")

    def test_claude_attachment_not_stored(self) -> None:
        attachment = {
            "type": "hook_additional_context",
            "content": ['<pctx-memory source="pctx">x</pctx-memory>'],
            "hookEvent": "SessionStart",
            "hookName": "SessionStart:startup",
            "toolUseID": "t",
        }
        records = [
            {"type": "attachment", "attachment": attachment, "uuid": "u"},
            {"type": "system", "content": "x", "isMeta": False},
            {"type": "summary", "summary": "x"},
            {"type": "ai-title", "title": "x"},
            {"type": "last-prompt", "sessionId": SESSION},
            {"type": "queue-operation", "sessionId": SESSION},
            claude_rec("user", "x") | {"message": "not-a-dict"},
            claude_rec("assistant", [text_block("x")])
            | {"message": {"role": "user", "content": "x"}},
        ]
        for record in records:
            with self.subTest(type=record["type"]):
                self.assertEqual(c.claude_events(record, 7), [])

    def test_multi_part_line_parts_numbered(self) -> None:
        blocks = [
            text_block("a"),
            tool_use("Bash", {"command": "ls"}, "toolu_1"),
            text_block("b"),
            tool_use("Read", {"file_path": "/x"}, "toolu_2"),
        ]
        events = c.claude_events(claude_rec("assistant", blocks), 7)
        self.assertEqual(
            events,
            [
                cev("reply", "a\nb", role="assistant"),
                cev(
                    "tool_call",
                    'Bash: {"command": "ls"}',
                    role="assistant",
                    tag="Bash",
                    part=2,
                    call_id="toolu_1",
                ),
                cev(
                    "tool_call",
                    'Read: {"file_path": "/x"}',
                    role="assistant",
                    tag="Read",
                    part=3,
                    call_id="toolu_2",
                ),
            ],
        )

    def test_claude_pasted_pctx_block_flag1(self) -> None:
        pasted = 'look: <pctx-memory source="pctx" trust="untrusted-data">'
        events = c.claude_events(claude_rec("user", pasted + " D"), 7)
        self.assertEqual(events, [cev("prompt", pasted + " D", flags=1)])
        notice = claude_rec("user", f'{{"notice": "{c.NOTICE}"}}')
        self.assertEqual(c.claude_events(notice, 7)[0].flags, 1)
