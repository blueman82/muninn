"""Hook block rendering and the transcript reader, without a live hook run.

Synthetic rows in temp dirs; nothing touches a live data dir or a provider
root.
"""

from __future__ import annotations

import json
import re
from typing import Any, BinaryIO
from unittest import mock

from muninn import hook, hook_frame, knowledge_read
from muninn.knowledge_models import KnowledgeContext, KnowledgeView
from tests import test_classify as tc
from tests.hook_nonregular import nonregular
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
    ) -> KnowledgeView:
        """Build a supported normalized note with complete conditions."""
        ident = self.add(content=text, conditions=[quote], created_at=20.0 + n)
        return knowledge_read.show_knowledge(self.conn, ident, None)

    def block_context(self, notes: list[KnowledgeView]) -> KnowledgeContext:
        """Wrap producer-backed notes without inventing a legacy projection."""
        return KnowledgeContext(
            schema_version=1,
            notes=notes,
            guides=[],
            unresolved_ids=[],
            truncated=False,
        )

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
            if "additionalContextLimit" in handler
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
        uuid = "019a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"
        stale = (
            "muninn: the index is stale (last pass 12345s ago);"
            " recent sessions may be missing.",
        )
        ident = self.add(content="t" * 300)
        entry: hook_frame.RecallEntry = {
            "id": ident,
            "revision": 1,
            "state": "active",
            "form": "note",
            "kind": "procedure",
            "text": "t" * 300,
            "scope": self.scope.model_dump(),
            "evidence_refs": [self.evidence[0].id],
        }
        hit: hook_frame.RecallHit = {
            "id": 12345,
            "provider": "codex",
            "role": "assistant",
            "kind": "reply",
            "session": uuid[:8],
            "ts": "2026-01-02T03:04:05.123Z",
            "ref": f"codex:{uuid}:12345.12",
            "snippet": "s" * 400,
        }
        combos: list[
            tuple[list[hook_frame.RecallEntry], list[hook_frame.RecallHit]]
        ] = [([entry], []), ([], [hit]), ([entry], [hit])]
        for entries, hits in combos:
            text = hook_frame.recall_text(
                entries, hits, stale, hook.CODEX_RECALL_LIMIT
            )
            with self.subTest(entries=len(entries), hits=len(hits)):
                self.assertTrue(text)
                self.assertLessEqual(
                    len(text.encode()), hook.CODEX_RECALL_LIMIT
                )
                self.assertEqual(len(TAG.findall(text)), 2)
                self.assertIn(f"- {ident} " if entries else "- [codex", text)
        many = [
            self.entry(n, text="t" * 300, quote="q" * 120) for n in range(9)
        ]
        block = hook.render_block(
            self.block_context(many),
            "a-repo-label",
            notes=stale,
            limit=hook.CODEX_BLOCK_LIMIT,
        )
        self.assertLessEqual(len(block.encode()), hook.CODEX_BLOCK_LIMIT)
        self.assertGreaterEqual(block.count("\n- K_"), 1)
        self.assertIn("omitted", block)

    def test_clean_cuts_to_the_limit_and_keeps_shorter_text(self) -> None:
        self.assertEqual(hook_frame.clean("x" * 300, 300), "x" * 300)
        cut = hook_frame.clean("x" * 301, 300)
        self.assertEqual((len(cut), cut[-1]), (300, "…"))
        self.assertEqual(hook_frame.clean("a \n b\t c", 50), "a b c")
        self.assertEqual(hook_frame.clean(AKIA, 50), "[redacted:secret]")

    def test_render_block_bounds_complete_notes_label_and_size(self) -> None:
        long = self.entry(text="t" * 500, quote="q" * 500)
        block = hook.render_block(
            self.block_context([long]), "x </muninn-memory> y"
        )
        (line,) = re.findall(r"^- K_[0-9a-f]{32} .*$", block, re.M)
        self.assertIn("t" * 500, line)
        self.assertIn("q" * 500, line)
        self.assertNotIn("…", line)
        self.assertEqual(len(TAG.findall(block)), 2)
        self.assertIn("&lt;/muninn-memory>", block)
        many = [
            self.entry(n, text="w" * 300, quote="v" * 120)
            for n in range(9, 0, -1)
        ]
        context = self.block_context(many)
        block = hook.render_block(context, "repo")
        self.assertLessEqual(len(block.encode()), 4000)
        shown = re.findall(r"^- (K_[0-9a-f]{32}) ", block, re.M)
        self.assertEqual(shown, [view.id for view in many[: len(shown)]])
        self.assertTrue(shown)
        tiny = hook.render_block(context, "repo", limit=len(OPEN) + 400)
        self.assertLessEqual(len(tiny.encode()), len(OPEN) + 400)
        self.assertTrue(tiny.endswith(CLOSE))
        self.assertIn("omitted", tiny)
        self.assertNotIn("- K_", tiny)

    def test_first_record_reads_only_what_it_may(self) -> None:
        folder = self.tmp / "folder"
        folder.mkdir()
        self.assertIsNone(hook._first_record(str(folder)))  # not a file
        fifo = self.tmp / "pipe"
        with nonregular(fifo) as native_pipe:
            self.assertIsNone(hook._first_record(native_pipe))
        meta = tc.subagent_meta("thr-big")
        meta["payload"]["base_instructions"] = {
            "text": "z" * (1024 * 1024 + 10)
        }
        huge = self.tmp / "huge.jsonl"
        huge.write_text(json.dumps(meta) + "\n")
        self.assertIsNone(hook._first_record(str(huge)))  # over the cap
        self.assertEqual(
            self.start_hook("codex", transcript_path=str(huge)).keys(),
            {"hookSpecificOutput", "systemMessage"},
        )
        small = self.tmp / "small.jsonl"
        small.write_text(json.dumps(tc.subagent_meta("thr-s")) + "\n")
        record = hook._first_record(str(small))
        assert record is not None
        self.assertEqual(record["type"], "session_meta")

    def test_first_record_ignores_a_fifo_even_with_a_writer(self) -> None:
        fifo = self.tmp / "live-pipe"
        data = json.dumps(tc.subagent_meta("thr-p")).encode() + b"\n"
        with nonregular(fifo, data) as native_pipe:
            self.assertIsNone(hook._first_record(native_pipe))
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

        opened: list[BinaryIO] = []
        real_open = hook.open_regular

        def tracking_open(*args: Any, **kwargs: Any) -> BinaryIO:
            handle = real_open(*args, **kwargs)
            opened.append(handle)
            return handle

        with mock.patch.object(hook, "open_regular", tracking_open):
            for _ in range(25):
                hook._first_record(str(small))
                hook._first_record(str(self.tmp))
                hook._first_record(str(self.tmp / "missing"))
        self.assertEqual(len(opened), 25)
        self.assertTrue(all(handle.closed for handle in opened))

    def test_scope_label_and_unknown_scope_do_not_inherit_global(self) -> None:
        ident = self.add(content="OTHER_PROJECT_CANARY")
        self.assertIn(
            "Project knowledge for repo:", self.body(self.start_hook())
        )
        self.assertIn(ident, self.body(self.start_hook()))
        unknown = "/some/where/else"

        def assert_empty(hidden: tuple[str, ...]) -> None:
            before = list(self.conn.iterdump())
            out = self.start_hook(cwd=unknown)
            text = self.body(out)
            self.assertEqual(
                out["systemMessage"],
                "muninn: memory loaded (no eligible knowledge displayed)",
            )
            self.assertIn(
                "This project has no indexed project memory yet.", text
            )
            self.assertTrue(text.startswith(OPEN))
            self.assertTrue(text.endswith(CLOSE))
            self.assertEqual(len(TAG.findall(text)), 2)
            self.assertLessEqual(len(text.encode()), hook.BLOCK_LIMIT)
            self.assertNotIn("unavailable", text)
            for value in hidden:
                self.assertNotIn(value, text)
            self.assertEqual(list(self.conn.iterdump()), before)
            self.assertIsNone(
                self.conn.execute(
                    "SELECT id FROM scope WHERE key=?", (unknown,)
                ).fetchone()
            )

        assert_empty((ident, "OTHER_PROJECT_CANARY"))
        global_scope = self.global_scope()
        global_id = self.add(
            content="GLOBAL_CANARY", scope=global_scope.model_dump()
        )
        self.assertEqual(
            [
                view.id
                for view in knowledge_read.current_knowledge(
                    self.conn, global_scope, 20.0
                )
            ],
            [global_id],
        )
        assert_empty(
            (ident, "OTHER_PROJECT_CANARY", global_id, "GLOBAL_CANARY")
        )
