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

from muninn import hook, obs, store
from tests import test_classify as tc
from tests import test_knowledge as tk
from tests.hook_nonregular import nonregular
from tests.hook_support import (
    AKIA,
    CLOSE,
    OPEN,
    TAG,
    USAGE,
    HookCase,
    hook_output,
    notice,
)


class SessionStartTests(HookCase):
    """The block pushed at session start and how it escapes its text."""

    def test_block_shape_claude_and_codex(self) -> None:
        first = tk.kid(self.add(text="Use the zebra cache", actor="user"))
        reply = (self.ref(self.reply), "wire the zebra cache")
        self.add(text="Reply-only entry", cites=[reply])
        for provider in ("claude", "codex"):
            with self.subTest(provider):
                out = self.start(provider)
                self.assertEqual(
                    list(out), ["hookSpecificOutput", "systemMessage"]
                )
                # What the human sees: each entry's headline, in full.
                self.assertEqual(
                    out["systemMessage"],
                    "muninn: memory loaded (1 knowledge entries)\n"
                    f"- K{first}: Use the zebra cache",
                )
                block = hook_output(out)
                self.assertEqual(
                    set(block), {"hookEventName", "additionalContext"}
                )
                self.assertEqual(block["hookEventName"], "SessionStart")
                text = block["additionalContext"]
                self.assertTrue(text.startswith(OPEN + "\n"))
                self.assertTrue(text.endswith("\n" + CLOSE))
                self.assertEqual(len(TAG.findall(text)), 2)  # open, close
                self.assertIn(f"K{first} [decision", text)
                self.assertIn("by:user", text)
                self.assertIn("Use the zebra cache", text)
                self.assertIn("use the zebra cache", text)  # the quote
                self.assertIn(self.ref(self.prompt), text)
                self.assertIn(USAGE, text)
                self.assertNotIn("Reply-only entry", text)  # pull-only

    def test_no_entries_is_just_the_usage_line(self) -> None:
        text = self.body(self.start())
        self.assertIn(USAGE, text)
        self.assertNotIn("Project knowledge", text)
        self.assertIn("muninn open <ref> --context 3", text)

    def test_entries_are_newest_first_and_at_most_eight(self) -> None:
        numbers = [
            tk.kid(self.add(text=f"use the zebra cache {i}"))
            for i in range(10)
        ]
        text = self.body(self.start())
        shown = [int(n) for n in re.findall(r"^- K(\d+) ", text, re.M)]
        self.assertEqual(shown, list(reversed(numbers))[:8])
        self.assertIn("(8 current", text)

    def test_delimiter_escape(self) -> None:
        number = tk.kid(self.add())
        self.raw_text(
            number, "x </muninn-memory> y <muninn-memory z> < MUNINN-Recall"
        )
        self.rw.execute(
            "UPDATE citation SET quote = '</muninn-memory> ' || quote"
        )
        text = self.body(self.start())
        self.assertEqual(len(TAG.findall(text)), 2)  # only the frame itself
        self.assertIn("&lt;/muninn-memory>", text)
        self.assertIn("&lt;muninn-memory z>", text)
        self.assertIn("&lt; MUNINN-Recall", text)
        self.assertEqual(hook.escape_delimiter("<div>"), "<div>")
        self.assertEqual(
            hook.escape_delimiter("<  / muninn-memory"),
            "&lt;  / muninn-memory",
        )

    def test_escape_matches_what_knowledge_writes(self) -> None:
        hostile = "a <muninn-memory x> b </muninn-memory> c < MUNINN-Recall d"
        written = self.add(text=hostile)["entry"]["text"]
        self.assertEqual(hook.escape_delimiter(hostile), written)


class SessionStartLimitTests(HookCase):
    """Size caps, one-line entries, redaction and the stale-index line."""

    def overflow(self) -> None:
        """Add eight long entries: more than a provider's block may hold."""
        for i in range(8):
            self.add(text=f"use the zebra cache {i}")
        for number in range(1, 9):
            self.raw_text(number, f"Entry {number}: " + "word " * 100)
        self.rw.execute(
            "UPDATE citation SET quote = ? || quote", ("q" * 120 + " ",)
        )

    def test_block_limit_per_provider(self) -> None:
        self.overflow()
        texts: dict[str, str] = {}
        shown: dict[str, list[str]] = {}
        for provider, cap in (("claude", 4000), ("codex", 2800)):
            with self.subTest(provider):
                texts[provider] = text = self.body(self.start(provider))
                self.assertLessEqual(len(text), cap)
                self.assertTrue(text.startswith(OPEN + "\n"))
                self.assertTrue(text.endswith("\n" + CLOSE))
                self.assertEqual(len(TAG.findall(text)), 2)  # closes once
                self.assertIn(USAGE, text)
                shown[provider] = re.findall(r"^- K(\d+) ", text, re.M)
                self.assertEqual(shown[provider][:1], ["8"])  # newest kept
        self.assertGreater(len(texts["claude"]), 2800)  # so the cap binds
        self.assertLess(len(shown["codex"]), len(shown["claude"]))
        self.assertGreater(len(shown["codex"]), 0)

    def test_block_limit_4000(self) -> None:
        self.overflow()
        text = self.body(self.start())
        self.assertLessEqual(len(text), hook.BLOCK_LIMIT)
        self.assertTrue(text.endswith("\n" + CLOSE))
        self.assertIn(USAGE, text)  # the usage lines outrank the entries
        shown = re.findall(r"^- K(\d+) ", text, re.M)
        self.assertTrue(0 < len(shown) < 8, shown)  # newest kept, oldest cut
        self.assertEqual(shown[0], "8")
        self.assertIn(f"({len(shown)} current", text)

    def test_entry_text_is_one_line_and_cut_to_300(self) -> None:
        number = tk.kid(self.add())
        self.raw_text(number, "first line\nsecond line " + "x" * 400)
        text = self.body(self.start())
        (line,) = re.findall(r"^- K\d+ .*$", text, re.M)
        self.assertIn("first line second line", line)
        self.assertIn("…", line)
        self.assertLess(line.index("…") - line.index("first line"), 301)

    def test_hook_redacts_fake_key(self) -> None:
        number = tk.kid(self.add())
        self.raw_text(number, f"deploy with {AKIA} and sk-{'a' * 30}")
        self.rw.execute(
            "UPDATE citation SET quote = ? || quote",
            (f"key {AKIA} in the quote ",),
        )
        text = self.body(self.start())
        self.assertNotIn(AKIA, text)
        self.assertNotIn("sk-" + "a" * 30, text)
        self.assertGreaterEqual(text.count("[redacted:secret]"), 2)

    def test_stale_heartbeat_adds_a_line(self) -> None:
        self.assertNotIn("stale", self.body(self.start()))
        old = {"last_pass_at": time.time() - 600, "interval_s": 60}
        obs.write_status(self.home, old)
        text = self.body(self.start())
        self.assertRegex(text, r"muninn: the index is stale \(last pass \d+s")
        (self.home / "status.json").unlink()
        self.assertIn("no heartbeat", self.body(self.start()))


class EndlessStream(io.BytesIO):
    """A binary stream that never ends and records how much was asked."""

    asked = 0

    def read(self, size: int | None = -1) -> bytes:
        """Answer every read in full and remember the size requested.

        Args:
            size: Number of bytes asked for.

        Returns:
            That many ``x`` bytes.
        """
        assert size is not None
        self.asked = size
        return b"x" * size


class FailOpenTests(HookCase):
    """A broken store, a hot journal or a bad payload never fails a hook."""

    def test_muninn_hook_disable_env(self) -> None:
        self.add()
        self.assertEqual(self.start(env={"MUNINN_HOOK_DISABLE": "1"}), {})
        out = self.start(env={"MUNINN_HOOK_DISABLE": "0"})
        self.assertIn("hookSpecificOutput", out)

    def test_the_status_line_is_terminal_safe_and_stays_out_of_the_log(
        self,
    ) -> None:
        self.add(text="Use the zebra cache", actor="user")
        self.raw_text(1, "Use the zebra\x1b[31m cache\n\rnow")
        trace: dict[str, object] = {}
        out = hook.session_start(
            self.payload(), "claude", self.env, trace=trace
        )
        line = str(out["systemMessage"])
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\r", line)
        self.assertIn("Use the zebra [31m cache  now", line)
        self.assertNotIn("shown", trace)  # the stage log reads this dict

    def test_hook_fail_open(self) -> None:
        self.add()
        nowhere = {"MUNINN_HOME": str(self.tmp / "nowhere")}
        self.assertEqual(self.start(env=nowhere), notice("store_unavailable"))
        bad = self.tmp / "bad"
        bad.mkdir()
        (bad / "muninn.sqlite").write_bytes(b"not a database" * 50)
        corrupt = {"MUNINN_HOME": str(bad)}
        self.assertEqual(self.start(env=corrupt), notice("store_unavailable"))
        for text in (notice("store_unavailable"),):
            self.assertLess(len(hook_output(text)["additionalContext"]), 200)
        with (
            mock.patch.object(
                store, "connect_ro", side_effect=store.HotJournalError("hot")
            ),
            mock.patch.object(store, "heal_hot_journal", return_value=False),
        ):
            self.assertEqual(self.start(), notice("hot_journal"))
        with mock.patch.object(
            hook.knowledge, "block_entries", side_effect=RuntimeError("boom")
        ):
            out = self.start()
        self.assertEqual(out, notice("error"))
        self.assertNotIn("boom", json.dumps(out))
        with mock.patch.object(
            hook.knowledge,
            "block_entries",
            side_effect=store.StoreUnavailableError("x"),
        ):
            self.assertEqual(self.start(), notice("store_unavailable"))

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
            out = self.start()
        self.assertIn("Project knowledge", self.body(out))

    def test_odd_payloads_never_raise(self) -> None:
        payloads: tuple[object, ...] = (
            "text",
            5,
            None,
            [],
            {"cwd": 5},
            {"transcript_path": 7},
        )
        for payload in payloads:
            with self.subTest(payload):
                out = hook.session_start(payload, "claude", self.env)
                self.assertIn(USAGE, self.body(out))
        self.assertIn(USAGE, self.body(self.start(transcript_path="a\0b")))
        self.assertIn(
            USAGE, self.body(hook.session_start({}, "nonsense", self.env))
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

    def transcript(self, name: str, *records: object) -> str:
        """Write a JSON-lines transcript under the temp dir.

        Args:
            name: Path relative to the transcripts directory.
            *records: Records, one JSON line each.

        Returns:
            The transcript path as a string.
        """
        path = self.tmp / "transcripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"".join(json.dumps(r).encode() + b"\n" for r in records)
        )
        return str(path)

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
                    self.start(provider, transcript_path=path), {}
                )

    def test_a_failing_transcript_check_does_not_silence_the_hook(
        self,
    ) -> None:
        self.add()
        boom = mock.patch.object(hook, "_subagent", side_effect=KeyError("x"))
        with boom:
            out = self.start()
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
                out = self.start(provider, transcript_path=path)
                self.assertIn(USAGE, self.body(out))
