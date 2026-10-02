"""Knowledge add with ``quote_only``: the caller's own recent prompts."""

from __future__ import annotations

from typing import Any
from unittest import mock

from muninn import ingest, knowledge, store
from tests import test_classify as tc
from tests import test_ingest as ti


class QuoteOnlyTests(ti.IngestCase):
    """``quote_only`` cites the caller's prompts after a targeted ingest."""

    QUOTE = "the zebra cache stays in place"

    def setUp(self) -> None:
        super().setUp()
        self.tid = ti.TID
        self.other_tid = "0199aaaa-bbbb-4ccc-8ddd-111111111111"
        self.third_tid = "0199aaaa-bbbb-4ccc-8ddd-222222222222"
        self.path = self.write(
            ti.rollout(self.tid),
            [
                tc.codex_meta("user", self.tid),
                tc.user_msg(1, f"we decided {self.QUOTE}, old wording"),
                tc.reply(2, f"noted: {self.QUOTE}"),
                tc.user_msg(
                    3, f"<user_instructions>{self.QUOTE}</user_instructions>"
                ),
            ],
        )
        self.write(
            ti.rollout(self.other_tid),
            [
                tc.codex_meta("user", self.other_tid),
                tc.user_msg(1, f"elsewhere: {self.QUOTE}"),
            ],
        )
        self.run_ingest()
        # typed a moment ago: on disk, not yet ingested
        self.append(
            self.path, [tc.user_msg(4, f"final call: {self.QUOTE}, ship it")]
        )
        self.write(
            ti.rollout(self.third_tid),
            [
                tc.codex_meta("user", self.third_tid),
                tc.user_msg(1, "never ingested"),
            ],
        )

    def add(self, **kw: Any) -> dict[str, Any]:
        """Call ``knowledge.add`` with a ``quote_only`` citation by default.

        Args:
            **kw: Arguments that override the defaults.

        Returns:
            The result of ``knowledge.add``.
        """
        args: dict[str, Any] = {
            "kind": "decision", "text": "The zebra cache stays",
            "cites": [], "quote_only": self.QUOTE, "supersedes": None,
            "global_scope": False, "cwd": tc.CWD, "actor": "codex:0199aaaa",
            "roots": self.roots, "env": {"CODEX_THREAD_ID": self.tid},
        }  # fmt: skip
        return knowledge.add(self.conn, **(args | kw))

    def test_quote_only_searches_callers_session_prompts(self) -> None:
        self.assertEqual(self.events()[-1][:3], (4, 1, "harness"))  # not yet
        got = self.add()
        (cite,) = got["entry"]["cites"]
        self.assertEqual(cite["role"], "user")
        self.assertEqual(cite["kind"], "prompt")
        self.assertEqual(cite["quote"], self.QUOTE)
        rows = {r[0]: r[3] for r in self.events()}
        newest = max(
            line for line, text in rows.items() if "final call" in text
        )
        self.assertEqual(cite["ref"], f"codex:{self.tid}:{newest}.1")
        start, end = cite["span"]
        self.assertEqual(rows[newest][start:end], self.QUOTE)
        self.assertEqual(got["entry"]["cites"][0]["verify"], "ok")
        # only the caller's threads were read: the third file stays unseen
        self.assertIsNone(self.source(self.third_tid))

    def test_quote_only_is_the_latest_prompt_never_reply_or_harness(
        self,
    ) -> None:
        self.append(self.path, [tc.reply(5, f"sure, {self.QUOTE}")])
        cite = self.add()["entry"]["cites"][0]
        self.assertEqual(cite["kind"], "prompt")
        self.assertTrue(cite["ref"].endswith(":5.1"))  # the appended prompt
        # the newer prompt shares the first word but not the quote: skipped
        only_old = self.add(quote_only="the zebra cache stays in place, old")
        self.assertTrue(only_old["entry"]["cites"][0]["ref"].endswith(":2.1"))

    def test_targeted_ingest_reaches_the_callers_sibling_threads(self) -> None:
        fork = "0199aaaa-bbbb-4ccc-8ddd-333333333333"
        self.write(
            ti.rollout(fork),
            [
                tc.codex_meta("user", fork, session_id=self.tid),
                tc.user_msg(1, "forked talk"),
            ],
        )
        self.run_ingest(only_threads={fork})  # known, in the session of tid
        self.append(self.path, [tc.user_msg(5, f"sibling: {self.QUOTE}")])
        got = self.add(
            env={"CODEX_THREAD_ID": fork}, quote_only=f"sibling: {self.QUOTE}"
        )
        ref = got["entry"]["cites"][0]["ref"]
        self.assertTrue(ref.startswith(f"codex:{self.tid}:"))

    def test_explicit_and_quote_only_citations_of_one_event_merge(
        self,
    ) -> None:
        got = self.add()
        (cite,) = got["entry"]["cites"]
        again = self.add(cites=[(cite["ref"], f"  {self.QUOTE}  ")])
        self.assertEqual(
            [c["ref"] for c in again["entry"]["cites"]], [cite["ref"]]
        )

    def test_quote_only_errors(self) -> None:
        self.refused_add("no_caller_session", env={})
        self.refused_add("no_caller_session", env={"CODEX_THREAD_ID": ""})
        self.refused_add("quote_not_found", quote_only="a quote nobody typed")
        self.refused_add("quote_length", quote_only="")
        self.refused_add("quote_length", quote_only="x" * 301)
        # another session's prompt is never used
        other = {"CODEX_THREAD_ID": self.other_tid}
        got = self.add(env=other, quote_only="elsewhere: the zebra cache")
        self.assertIn(self.other_tid, got["entry"]["cites"][0]["ref"])
        self.refused_add(
            "quote_not_found",
            quote_only=f"final call: {self.QUOTE}",
            env=other,
        )

    def refused_add(self, code: str, **kw: Any) -> None:
        """Assert that ``add`` refuses with ``code``.

        Args:
            code: The expected refusal code.
            **kw: Arguments that override the ``add`` defaults.
        """
        with self.assertRaises(knowledge.RefusedError) as caught:
            self.add(**kw)
        self.assertEqual(caught.exception.code, code)

    def test_targeted_ingest_same_connection_no_relock(self) -> None:
        with (
            store.writer_lock(self.home, wait_s=0),  # as the CLI holds it
            mock.patch.object(ingest, "ingest", wraps=ingest.ingest) as spy,
            mock.patch.object(
                ingest, "run_pass", side_effect=AssertionError("relocked")
            ),
        ):
            self.add()
        spy.assert_called_once()
        self.assertIs(spy.call_args.args[0], self.conn)
        self.assertEqual(spy.call_args.args[1], self.roots)
        self.assertEqual(spy.call_args.kwargs["only_threads"], {self.tid})
        with store.writer_lock(self.home, wait_s=0):  # released afterwards
            pass

    def test_ingest_only_with_a_caller_and_roots(self) -> None:
        with mock.patch.object(ingest, "ingest") as spy:
            self.add(
                cites=[
                    (f"codex:{self.tid}:2.1", "we decided the zebra cache")
                ],
                quote_only=None,
                env={},
            )
            self.add(
                cites=[
                    (f"codex:{self.tid}:2.1", "we decided the zebra cache")
                ],
                quote_only=None,
                roots={},
            )
        spy.assert_not_called()

    def test_claude_caller(self) -> None:
        main = f"-work-repo/{tc.SESSION}.jsonl"
        path = self.write(
            main,
            [tc.claude_rec("user", "earlier talk")],
            root="claude-projects",
        )
        self.run_ingest()
        self.append(
            path, [tc.claude_rec("user", f"remember {self.QUOTE} please")]
        )
        env = {"CLAUDE_CODE_SESSION_ID": tc.SESSION}
        cite = self.add(env=env)["entry"]["cites"][0]
        self.assertEqual(cite["ref"], f"claude:{tc.SESSION}:2.1")
        self.assertEqual(cite["quote"], self.QUOTE)
