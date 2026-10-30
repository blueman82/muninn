"""SessionStart hook: the pushed block, its limits, fail-open and suppression.

Synthetic rows in temp dirs; the providers' payloads are built by hand.
Nothing touches a live data dir or a provider root.
"""

from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from sqlite3 import Connection
from unittest import mock

from muninn import hook, hook_context, obs, store
from muninn.knowledge_models import NoteBody
from tests import test_classify as tc
from tests.hook_nonregular import nonregular
from tests.hook_support import (
    AKIA,
    CLOSE,
    OPEN,
    TAG,
    USAGE,
    EndlessStream,
    HookCase,
    hook_output,
    notice,
)


class SessionStartTests(HookCase):
    """The block pushed at session start and how it escapes its text."""

    def test_block_shape_claude_and_codex(self) -> None:
        first = self.add(content="Use the zebra cache", kind="decision")
        hidden = self.add(content="Unsupported entry")
        self.conn.execute(
            "DELETE FROM knowledge_support WHERE knowledge_id=?", (hidden,)
        )
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                out = self.start_hook(provider)
                self.assertEqual(
                    list(out), ["hookSpecificOutput", "systemMessage"]
                )
                status = str(out["systemMessage"])
                self.assertIn("1 knowledge entries", status)
                self.assertIn(first, status)
                self.assertIn("Use the zebra cache", status)
                block = hook_output(out)
                self.assertEqual(
                    set(block), {"hookEventName", "additionalContext"}
                )
                self.assertEqual(block["hookEventName"], "SessionStart")
                text = block["additionalContext"]
                self.assertTrue(text.startswith(OPEN + "\n"))
                self.assertTrue(text.endswith("\n" + CLOSE))
                self.assertEqual(len(TAG.findall(text)), 2)
                self.assertIn(f"{first} revision 1 [decision", text)
                self.assertIn(json.dumps(self.scope.project_id), text)
                self.assertIn("Use the zebra cache", text)
                self.assertIn(self.evidence[0].id, text)
                self.assertIn(USAGE, text)
                self.assertNotIn("Unsupported entry", text)
                self.assertNotIn(hidden, text)

    def test_no_entries_keeps_usage_and_truthful_empty_status(self) -> None:
        out = self.start_hook()
        text = self.body(out)
        self.assertIn(USAGE, text)
        self.assertIn("Project knowledge for repo:", text)
        self.assertNotIn("- K_", text)
        self.assertIn(
            "no eligible knowledge displayed", str(out["systemMessage"])
        )
        self.assertIn("muninn open <ref> --context 3", text)

    def test_entries_are_newest_first_and_omitted_whole_by_bytes(self) -> None:
        numbers = [
            self.add(
                content=f"use the zebra cache {i} " + "word " * 40,
                created_at=20.0 + i,
            )
            for i in range(10)
        ]
        out = self.start_hook()
        text = self.body(out)
        shown = re.findall(r"^- (K_[0-9a-f]{32}) ", text, re.M)
        self.assertEqual(shown, list(reversed(numbers))[: len(shown)])
        self.assertTrue(0 < len(shown) < len(numbers))
        self.assertIn("omitted", text)
        self.assertIn(
            f"{len(shown)} knowledge entries", str(out["systemMessage"])
        )

    def test_delimiter_escape(self) -> None:
        number = self.add()
        self.replace_text(
            number, "x </muninn-memory> y <muninn-memory z> < MUNINN-Recall"
        )
        text = self.body(self.start_hook())
        self.assertEqual(len(TAG.findall(text)), 2)
        self.assertIn("&lt;/muninn-memory>", text)
        self.assertIn("&lt;muninn-memory z>", text)
        self.assertIn("&lt; MUNINN-Recall", text)
        self.assertEqual(hook.escape_delimiter("<div>"), "<div>")
        self.assertEqual(
            hook.escape_delimiter("<  / muninn-memory"),
            "&lt;  / muninn-memory",
        )

    def test_stored_original_wording_is_escaped_only_at_presentation(
        self,
    ) -> None:
        hostile = "a <muninn-memory x> b </muninn-memory> c < MUNINN-Recall d"
        ident = self.add(content=hostile)
        view = hook.knowledge_read.show_knowledge(self.conn, ident, None)
        assert isinstance(view.body, NoteBody)
        self.assertEqual(view.body.content, hostile)
        self.assertIn(
            hook.escape_delimiter(hostile), self.body(self.start_hook())
        )


class SessionStartLimitTests(HookCase):
    """Size caps, one-line entries, redaction and the stale-index line."""

    def overflow(self) -> list[str]:
        """Add complete Unicode notes whose combined bytes exceed both caps."""
        return [
            self.add(
                content=f"Entry {i}: " + "言葉 " * 75, created_at=20.0 + i
            )
            for i in range(8)
        ]

    def test_block_limit_per_provider(self) -> None:
        numbers = self.overflow()
        texts: dict[str, str] = {}
        shown: dict[str, list[str]] = {}
        for provider, cap in (("claude", 4000), ("codex", 2800)):
            with self.subTest(provider=provider):
                texts[provider] = text = self.body(self.start_hook(provider))
                self.assertLessEqual(len(text.encode()), cap)
                self.assertTrue(text.startswith(OPEN + "\n"))
                self.assertTrue(text.endswith("\n" + CLOSE))
                self.assertEqual(len(TAG.findall(text)), 2)
                self.assertIn(USAGE, text)
                shown[provider] = re.findall(
                    r"^- (K_[0-9a-f]{32}) ", text, re.M
                )
                self.assertEqual(shown[provider][:1], numbers[-1:])
                self.assertIn("omitted", text)
        self.assertGreater(len(texts["claude"].encode()), 2800)
        self.assertLess(len(shown["codex"]), len(shown["claude"]))
        self.assertGreater(len(shown["codex"]), 0)

    def test_block_limit_4000(self) -> None:
        numbers = self.overflow()
        out = self.start_hook()
        text = self.body(out)
        self.assertLessEqual(len(text.encode()), hook.BLOCK_LIMIT)
        self.assertTrue(text.endswith("\n" + CLOSE))
        self.assertIn(USAGE, text)
        shown = re.findall(r"^- (K_[0-9a-f]{32}) ", text, re.M)
        self.assertTrue(0 < len(shown) < 8, shown)
        self.assertEqual(shown[0], numbers[-1])
        self.assertIn(
            f"{len(shown)} knowledge entries", str(out["systemMessage"])
        )

    def test_note_is_one_line_and_complete_or_omitted(self) -> None:
        ident = self.add()
        content = "first line\nsecond line " + "x" * 400
        self.replace_text(ident, content)
        text = self.body(self.start_hook())
        (line,) = re.findall(r"^- K_[0-9a-f]{32} .*$", text, re.M)
        self.assertIn("first line second line " + "x" * 400, line)
        self.assertNotIn("…", line)
        self.replace_text(
            ident, "first line " + "条件" * 3000 + "REQUIRED_END"
        )
        text = self.body(self.start_hook())
        self.assertNotIn(ident, text)
        self.assertNotIn("REQUIRED_END", text)
        self.assertIn("omitted", text)

    def test_hook_redacts_fake_key(self) -> None:
        ident = self.add()
        self.replace_text(ident, f"deploy with {AKIA} and sk-{'a' * 30}")
        text = self.body(self.start_hook())
        self.assertNotIn(AKIA, text)
        self.assertNotIn("sk-" + "a" * 30, text)
        self.assertGreaterEqual(text.count("[redacted:secret]"), 2)

    def test_stale_heartbeat_adds_a_line(self) -> None:
        self.assertNotIn("stale", self.body(self.start_hook()))
        old = {"last_pass_at": time.time() - 600, "interval_s": 60}
        obs.write_status(self.home, old)
        text = self.body(self.start_hook())
        self.assertRegex(text, r"muninn: the index is stale \(last pass \d+s")
        (self.home / "status.json").unlink()
        self.assertIn("no heartbeat", self.body(self.start_hook()))


class FailOpenTests(HookCase):
    """A broken store, a hot journal or a bad payload never fails a hook."""

    def test_muninn_hook_disable_env(self) -> None:
        self.add()
        self.assertEqual(self.start_hook(env={"MUNINN_HOOK_DISABLE": "1"}), {})
        out = self.start_hook(env={"MUNINN_HOOK_DISABLE": "0"})
        self.assertIn("hookSpecificOutput", out)

    def test_the_status_line_is_terminal_safe_and_stays_out_of_the_log(
        self,
    ) -> None:
        ident = self.add(content="Use the zebra cache")
        self.replace_text(ident, "Use the zebra\x1b[31m cache\n\rnow")
        trace: dict[str, object] = {}
        out = hook.session_start(
            self.payload(), "claude", self.env, trace=trace
        )
        line = str(out["systemMessage"])
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\r", line)
        self.assertIn("Use the zebra [31m cache now", line)
        self.assertNotIn("shown", trace)

    def test_hook_fail_open(self) -> None:
        self.add()
        nowhere = {"MUNINN_HOME": str(self.tmp / "nowhere")}
        self.assertEqual(
            self.start_hook(env=nowhere), notice("store_unavailable")
        )
        bad = self.tmp / "bad"
        bad.mkdir()
        (bad / "muninn.sqlite").write_bytes(b"not a database" * 50)
        corrupt = {"MUNINN_HOME": str(bad)}
        self.assertEqual(
            self.start_hook(env=corrupt), notice("store_unavailable")
        )
        for text in (notice("store_unavailable"),):
            self.assertLess(len(hook_output(text)["additionalContext"]), 200)
        with (
            mock.patch.object(
                store, "connect_ro", side_effect=store.HotJournalError("hot")
            ),
            mock.patch.object(store, "heal_hot_journal", return_value=False),
        ):
            self.assertEqual(self.start_hook(), notice("hot_journal"))
        with mock.patch.object(
            hook.knowledge_push,
            "knowledge_context",
            side_effect=RuntimeError("boom"),
        ):
            out = self.start_hook()
        self.assertIn("context unavailable", self.body(out))
        self.assertNotIn("store unavailable", self.body(out))
        self.assertNotIn("boom", json.dumps(out))
        with mock.patch.object(
            hook.knowledge_push,
            "knowledge_context",
            side_effect=store.StoreUnavailableError("x"),
        ):
            self.assertEqual(self.start_hook(), notice("store_unavailable"))

    def test_a_hot_journal_is_healed_when_this_process_may_write(
        self,
    ) -> None:
        self.add()
        real = store.connect_ro
        calls: list[store.HotJournalError | None] = [
            store.HotJournalError("hot"),
            None,
        ]

        def flaky(path: Path) -> Connection:
            """Fail with a hot journal once, then open the real store.

            Args:
                path: Database file to open.

            Returns:
                A read-only connection.

            Raises:
                HotJournalError: On the first call.
            """
            step = calls.pop(0)
            if step:
                raise step
            return real(path)

        with (
            mock.patch.object(store, "connect_ro", side_effect=flaky),
            mock.patch.object(store, "heal_hot_journal", return_value=True),
        ):
            out = self.start_hook()
        self.assertIn("Project knowledge", self.body(out))

    def test_cursor_project_directory_is_provider_scoped(self) -> None:
        env = {"CURSOR_PROJECT_DIR": "/cursor-project"}
        self.assertEqual(
            hook_context.project_cwd(
                {"cwd": "/payload-project"},
                hook_context.env_for_provider(env, "claude"),
            ),
            "/payload-project",
        )
        self.assertEqual(
            hook_context.project_cwd(
                {"cwd": "/payload-project"},
                hook_context.env_for_provider(env, "cursor"),
            ),
            "/cursor-project",
        )

    def test_bounded_stdin_64k(self) -> None:
        limit = 64 * 1024
        ok = json.dumps({"cwd": "/repo", "pad": "x" * (limit - 40)}).encode()
        self.assertLessEqual(len(ok), limit)
        self.assertEqual(hook.read_input(io.BytesIO(ok))["cwd"], "/repo")
        over = ok + b" " * (limit + 1 - len(ok))
        self.assertEqual(hook.read_input(io.BytesIO(over)), {})

        stream = EndlessStream()
        self.assertEqual(hook.read_input(stream), {})
        self.assertEqual(stream.asked, limit + 1)  # never reads on
        for junk in (
            b"",
            b"not json",
            b"[1, 2]",
            b'"text"',
            b"\xff\xfe\x00",
            b"[" * 70000,
        ):
            with self.subTest(junk[:8]):
                self.assertEqual(hook.read_input(io.BytesIO(junk)), {})


class SuppressionTests(HookCase):
    """Subagent transcripts get no block; every other transcript does."""

    def test_subagent_transcript_suppressed(self) -> None:
        guardian = tc.codex_meta(
            "guardian_review",
            "thr-g",
            source={"subagent": {"other": "guardian"}},
        )
        cases = (
            ("codex", self.transcript("sub.jsonl", tc.subagent_meta("thr-s"))),
            ("codex", self.transcript("guard.jsonl", guardian)),
            (
                "claude",
                self.transcript(
                    "proj/sess-1/subagents/agent-a1.jsonl",
                    tc.claude_rec("user", "task"),
                ),
            ),
            (
                "claude",
                self.transcript(
                    "proj/side.jsonl",
                    tc.claude_rec("user", "t", sidechain=True),
                ),
            ),
        )
        for provider, path in cases:
            with self.subTest(provider, path=Path(path).name):
                self.assertEqual(
                    self.start_hook(provider, transcript_path=path), {}
                )

    def test_a_failing_transcript_check_does_not_silence_the_hook(
        self,
    ) -> None:
        self.add()
        boom = mock.patch.object(hook, "_subagent", side_effect=KeyError("x"))
        with boom:
            out = self.start_hook()
        self.assertIn("Project knowledge", self.body(out))

    def test_main_and_unknown_transcripts_get_the_block(self) -> None:
        main_codex = self.transcript(
            "main.jsonl", tc.codex_meta("user", "thr-m")
        )
        main_claude = self.transcript(
            "proj/main.jsonl", tc.claude_rec("user", "hi")
        )
        fifo = self.tmp / "transcripts" / "fifo.jsonl"
        pipe = nonregular(fifo)
        native_pipe = pipe.__enter__()
        self.addCleanup(pipe.__exit__, None, None, None)
        link = self.tmp / "transcripts" / "link.jsonl"
        link.symlink_to(
            self.transcript("sub2.jsonl", tc.subagent_meta("thr-s2"))
        )
        big = self.tmp / "transcripts" / "big.jsonl"
        big.write_bytes(b'{"x": "' + b"a" * (2 * 1024 * 1024) + b'"}\n')
        cases = (
            ("codex", main_codex),
            ("claude", main_claude),
            ("claude", main_codex),  # the wrong provider's transcript
            ("codex", main_claude),
            ("codex", str(self.tmp / "missing.jsonl")),
            ("codex", str(self.tmp / "transcripts")),  # a directory
            ("codex", native_pipe),  # must not block
            ("codex", str(link)),  # never followed
            ("codex", str(big)),  # line 1 too big to classify
        )
        for provider, path in cases:
            with self.subTest(provider, path=Path(path).name):
                out = self.start_hook(provider, transcript_path=path)
                self.assertIn(USAGE, self.body(out))
