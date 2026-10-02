"""Hook block rendering and the transcript reader, without a live hook run.

Synthetic rows in temp dirs; nothing touches a live data dir or a provider
root.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pctx import hook, hook_frame
from tests import test_classify as tc
from tests import test_knowledge as tk
from tests.hook_support import (
    AKIA,
    CLOSE,
    HOOKS_JSON,
    OPEN,
    TAG,
    HookCase,
)


class RenderTests(HookCase):
    """render_block and the helpers on their own, without a store."""

    def entry(
        self,
        n: int = 1,
        text: str = "a decision",
        quote: str = "verbatim words here",
    ) -> hook_frame.BlockEntry:
        """Build a SessionStart block entry.

        Args:
            n: Number in the entry id ``K<n>``.
            text: Entry text.
            quote: Quoted citation words.

        Returns:
            The entry.
        """
        return {
            "id": f"K{n}",
            "kind": "decision",
            "text": text,
            "actor": "user",
            "date": "2026-09-30",
            "cite": "codex:thr:1.1",
            "quote": quote,
        }

    def test_limits_are_the_documented_ones(self) -> None:
        self.assertEqual((hook.BLOCK_LIMIT, hook.RECALL_LIMIT), (4000, 1500))
        self.assertEqual(
            (hook.CODEX_BLOCK_LIMIT, hook.CODEX_RECALL_LIMIT), (2800, 900)
        )
        self.assertEqual(
            (hook.MAX_INPUT, hook.FIRST_LINE), (64 * 1024, 1024 * 1024)
        )
        self.assertEqual((hook.MIN_TERMS, hook.MAX_EVENTS), (3, 3))

    def test_codex_caps_fit_its_token_limits_at_under_two_chars_a_token(
        self,
    ) -> None:
        # Codex counts additionalContext in tokens; ids and timestamps cost
        # about 1.8 characters a token, so its blocks are capped lower.
        tokens = {
            event: handler["additionalContextLimit"]
            for event, groups in json.loads(HOOKS_JSON.read_text())[
                "hooks"
            ].items()
            for group in groups
            for handler in group["hooks"]
        }
        caps = {
            "SessionStart": hook.CODEX_BLOCK_LIMIT,
            "UserPromptSubmit": hook.CODEX_RECALL_LIMIT,
        }
        self.assertEqual(set(tokens), set(caps))
        for event, cap in caps.items():
            with self.subTest(event):
                self.assertLessEqual(cap / tokens[event], 1.9)

    def test_codex_caps_keep_room_for_the_fixed_lines_and_a_full_item(
        self,
    ) -> None:
        # The floor: frame, notice, hints and a stale line, plus the longest
        # realistic entry or hit (uuid refs), must still fit, or Codex would
        # silently get nothing.
        uuid = "019a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"
        stale = (
            "pctx: the index is stale (last pass 12345s ago);"
            " recent sessions may be missing.",
        )
        entry: hook_frame.RecallEntry = {
            "id": "K12345", "kind": "procedure", "date": "2026-09-30",
            "actor": "claude:abc123def456", "text": "t" * 300,
            "cites": [f"codex:{uuid}:12345.1", f"claude:{uuid}:12345.1"],
        }  # fmt: skip
        hit: hook_frame.RecallHit = {
            "id": 12345, "provider": "codex", "role": "assistant",
            "kind": "reply", "session": uuid[:8],
            "ts": "2026-01-02T03:04:05.123Z",
            "ref": f"codex:{uuid}:12345.12", "snippet": "s" * 400,
        }  # fmt: skip
        combos: list[
            tuple[list[hook_frame.RecallEntry], list[hook_frame.RecallHit]]
        ] = [([entry], []), ([], [hit]), ([entry], [hit])]
        for entries, hits in combos:
            text = hook_frame.recall_text(
                entries, hits, stale, hook.CODEX_RECALL_LIMIT
            )
            with self.subTest(len(text)):
                self.assertTrue(text)
                self.assertLessEqual(len(text), hook.CODEX_RECALL_LIMIT)
                self.assertEqual(len(TAG.findall(text)), 2)
                self.assertIn("- K12345 " if entries else "- [codex", text)
        many = [
            self.entry(n, text="t" * 300, quote="q" * 120) for n in range(9)
        ]
        block = hook.render_block(
            many, "a-repo-label", notes=stale, limit=hook.CODEX_BLOCK_LIMIT
        )
        self.assertLessEqual(len(block), hook.CODEX_BLOCK_LIMIT)
        self.assertGreaterEqual(block.count("\n- K"), 3)

    def test_clean_cuts_to_the_limit_and_keeps_shorter_text(self) -> None:
        self.assertEqual(hook_frame.clean("x" * 300, 300), "x" * 300)
        cut = hook_frame.clean("x" * 301, 300)
        self.assertEqual((len(cut), cut[-1]), (300, "…"))
        self.assertEqual(hook_frame.clean("a \n b\t c", 50), "a b c")
        self.assertEqual(hook_frame.clean(AKIA, 50), "[redacted:secret]")

    def test_render_block_bounds_quote_text_label_and_size(self) -> None:
        long = self.entry(text="t" * 500, quote="q" * 500)
        block = hook.render_block([long], "x </pctx-memory> y")
        (line,) = re.findall(r"^- K1 .*$", block, re.M)
        self.assertIn("t" * 299 + "…", line)
        self.assertIn('"' + "q" * 119 + '…"', line)
        self.assertEqual(len(TAG.findall(block)), 2)  # the label is escaped
        self.assertIn("&lt;/pctx-memory>", block)
        many = [
            self.entry(n, text="w" * 300, quote="v" * 120)
            for n in range(9, 0, -1)
        ]
        block = hook.render_block(many, "repo")
        self.assertLessEqual(len(block), 4000)
        shown = re.findall(r"^- K(\d+) ", block, re.M)
        self.assertEqual(shown, [str(n) for n in range(9, 9 - len(shown), -1)])
        tiny = hook.render_block(many, "repo", limit=len(OPEN) + 400)
        self.assertLessEqual(len(tiny), len(OPEN) + 400)
        self.assertTrue(tiny.endswith(CLOSE))

    def test_first_record_reads_only_what_it_may(self) -> None:
        folder = self.tmp / "folder"
        folder.mkdir()
        self.assertIsNone(hook._first_record(str(folder)))  # not a file
        fifo = self.tmp / "pipe"
        os.mkfifo(fifo)
        self.assertIsNone(hook._first_record(str(fifo)))
        meta = tc.subagent_meta("thr-big")
        meta["payload"]["base_instructions"] = {
            "text": "z" * (1024 * 1024 + 10)
        }
        huge = self.tmp / "huge.jsonl"
        huge.write_text(json.dumps(meta) + "\n")
        self.assertIsNone(hook._first_record(str(huge)))  # over the cap
        self.assertEqual(
            self.start("codex", transcript_path=str(huge)).keys(),
            {"hookSpecificOutput", "systemMessage"},
        )
        small = self.tmp / "small.jsonl"
        small.write_text(json.dumps(tc.subagent_meta("thr-s")) + "\n")
        record = hook._first_record(str(small))
        assert record is not None
        self.assertEqual(record["type"], "session_meta")

    def test_first_record_ignores_a_fifo_even_with_a_writer(self) -> None:
        fifo = self.tmp / "live-pipe"
        os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDWR)  # both ends open: a read would work
        self.addCleanup(os.close, fd)
        os.write(fd, json.dumps(tc.subagent_meta("thr-p")).encode() + b"\n")
        self.assertIsNone(hook._first_record(str(fifo)))
        self.assertIsNone(hook._first_record("a\0b"))  # not a path

    def test_first_record_line_cap_is_exact(self) -> None:
        meta: dict[str, Any] = tc.subagent_meta("thr-edge")
        meta["payload"]["pad"] = ""
        room = hook.FIRST_LINE - len(json.dumps(meta))
        for extra, accepted in ((0, True), (1, False)):
            meta["payload"]["pad"] = "z" * (room + extra)
            self.assertEqual(len(json.dumps(meta)), hook.FIRST_LINE + extra)
            path = self.tmp / f"edge{extra}.jsonl"
            path.write_text(json.dumps(meta) + "\n")
            found = hook._first_record(str(path))
            self.assertEqual(found is not None, accepted, extra)

    def test_first_record_closes_the_descriptor_it_opened(self) -> None:
        small = self.tmp / "small.jsonl"
        small.write_text(json.dumps(tc.subagent_meta("thr-s")) + "\n")

        def open_fds() -> int:
            """Count this process's open file descriptors.

            Returns:
                The number of entries in ``/dev/fd``.
            """
            return len(list(Path("/dev/fd").iterdir()))

        before = open_fds()
        for _ in range(25):
            hook._first_record(str(small))  # read
            hook._first_record(str(self.tmp))  # refused after the open
            hook._first_record(str(self.tmp / "missing"))  # never opened
        self.assertEqual(open_fds(), before)

    def test_scope_label_in_the_header_and_its_fallback(self) -> None:
        self.add()
        self.assertIn(
            "Project knowledge for repo (1 current", self.body(self.start())
        )
        wide = tk.kid(self.add(global_scope=True))
        out = self.start(cwd="/some/where/else")  # an unknown scope
        self.assertIn("Project knowledge for else (1 current", self.body(out))
        self.assertIn(f"K{wide} ", self.body(out))
