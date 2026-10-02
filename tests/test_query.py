"""Query contract: refs, term building, caller root, evidence marks, quotes."""

from __future__ import annotations

import time
import unittest
from typing import Any, cast

from muninn import classify, query
from tests.query_support import QueryCase


class RefTests(unittest.TestCase):
    """Reference parsing: ``provider:thread:line.part`` forms and limits."""

    def test_parse_ref_forms(self) -> None:
        self.assertEqual(
            query.parse_ref("codex:019a-b:12.3"), ("codex", "019a-b", 12, 3)
        )
        self.assertEqual(
            query.parse_ref(" claude:abc:7 "), ("claude", "abc", 7, 1)
        )
        for bad in ("", "12", "codex:12.1", "gemini:abc:1.1", "codex:a:x.1"):
            with self.subTest(bad), self.assertRaises(ValueError):
                query.parse_ref(bad)
        for zero in ("codex:abc:0.1", "codex:abc:1.0"):  # 1-based
            with self.subTest(zero), self.assertRaises(ValueError):
                query.parse_ref(zero)

    def test_parse_ref_rejects_absurd_numbers(self) -> None:
        for bad in (
            "codex:abc:99999999999999999999.1",
            "codex:abc:1.9999999999",
        ):
            with self.subTest(bad), self.assertRaises(ValueError):
                query.parse_ref(bad)


class BuildQueryTests(unittest.TestCase):
    """FTS query building: OR-joined, quoted, bounded terms."""

    def terms(self, text: str) -> list[str] | None:
        """Split the built query into its terms.

        Args:
            text: The raw search text.

        Returns:
            The quoted terms and phrases, or None when none survive.
        """
        built = query.build_fts_query(text)
        return None if built is None else built.split(" OR ")

    def test_terms_lowercase_quoted_or_joined(self) -> None:
        self.assertEqual(
            query.build_fts_query("Alpha  BETA"), '"alpha" OR "beta"'
        )

    def test_stopwords_short_terms_and_duplicates_dropped(self) -> None:
        got = self.terms("The plan of the PLAN is a b to 7 ok")
        self.assertEqual(got, ['"plan"', '"ok"'])

    def test_every_stopword_is_dropped(self) -> None:
        self.assertTrue(30 <= len(query.STOPWORDS) <= 45)  # "about 35"
        for word in query.STOPWORDS | {"AND", "Or", "NOT", "The"}:
            with self.subTest(word):
                self.assertIsNone(query.build_fts_query(word))
        self.assertEqual(
            self.terms("what is the plan and not the cache"),
            ['"plan"', '"cache"'],
        )

    def test_none_when_no_terms(self) -> None:
        for text in ("", "   ", "the a of to", "!!! ??? ...", "__ _ ___"):
            with self.subTest(text):
                self.assertIsNone(query.build_fts_query(text))

    def test_at_most_16_terms_in_order(self) -> None:
        words = [f"w{n:02d}" for n in range(30)]
        got = self.terms(" ".join(words))
        self.assertEqual(got, [f'"{w}"' for w in words[:16]])

    def test_quoted_phrase_replaces_its_words(self) -> None:
        got = self.terms('find "State  Machine" bug')
        self.assertEqual(got, ['"find"', '"bug"', '"state machine"'])
        self.assertEqual(self.terms('"one"'), ['"one"'])  # 1 word: a term

    def test_identifier_adds_phrase_of_parts(self) -> None:
        got = self.terms("fix hook_core.py now")
        self.assertEqual(
            got,
            ['"fix"', '"hook_core"', '"py"', '"now"', '"hook core py"'],
        )
        path = self.terms("see muninn/query.py:12")
        assert path is not None
        self.assertIn('"muninn query py 12"', path)

    def test_hostile_input_stays_linear(self) -> None:
        blobs = [
            "a" * 40000,
            "a_" * 20000,
            "a.b." * 10000,
            "x " * 20000,
            '"' * 40000,
            "a:b" * 10000,
            "-".join(["w"] * 10000),
            "é" * 40000,
        ]
        start = time.monotonic()
        for blob in blobs:
            query.build_fts_query(blob)
        self.assertLess(time.monotonic() - start, 2)

    def test_blobs_and_long_identifiers_are_bounded(self) -> None:
        self.assertIsNone(query.build_fts_query("a" * 101))  # a blob
        self.assertEqual(self.terms("a" * 100), ['"' + "a" * 100 + '"'])
        path = "/".join(f"seg{i}" for i in range(30))
        found = self.terms(path)
        assert found is not None
        phrase = found[-1]
        self.assertEqual(phrase.count(" "), 11)  # 12 parts, then cut
        many = " ".join(f"a{i}.b{i}" for i in range(20))
        self.assertEqual(
            len(self.terms(many) or []), 16 + 8
        )  # terms + phrases

    def test_every_term_is_a_safe_quoted_string(self) -> None:
        hostile = [
            'foo" OR (bar',
            "a:b * -c NEAR(x y) AND NOT z",
            '"unbalanced',
            "x^y {z} [w] ~t",
            "naïve café über 日本語",
        ]
        for text in hostile:
            with self.subTest(text):
                for term in self.terms(text) or []:
                    self.assertRegex(term, r'^"[^"]+"$')
                    self.assertTrue(any(c.isalnum() for c in term))


class CallerRootTests(QueryCase):
    """Which session root the calling process belongs to."""

    def test_env_precedence_and_thread_mapping(self) -> None:
        self.add_source("thr2", session="root1")

        def root(env: dict[str, str]) -> str | None:
            """Resolve the caller root for one environment.

            Args:
                env: The caller's environment variables.

            Returns:
                The session root, or None when the caller is unknown.
            """
            return query.caller_root(self.ro(), env)

        self.assertIsNone(root({}))
        self.assertIsNone(root({"CLAUDE_CODE_SESSION_ID": ""}))
        self.assertEqual(root({"CLAUDE_CODE_SESSION_ID": "c1"}), "c1")
        both = {"CLAUDE_CODE_SESSION_ID": "c1", "CODEX_SESSION_ID": "x1"}
        self.assertEqual(root(both), "c1")  # Claude first
        self.assertEqual(root({"CODEX_SESSION_ID": "x1"}), "x1")
        codex = {"CODEX_SESSION_ID": "x1", "CODEX_THREAD_ID": "thr2"}
        self.assertEqual(root(codex), "x1")  # session id before thread id
        self.assertEqual(root({"CODEX_THREAD_ID": "thr2"}), "root1")
        self.assertEqual(root({"CODEX_THREAD_ID": "new"}), "new")  # unmapped


class AnswerEvidenceTests(QueryCase):
    """Only primary prompts and replies count as answer evidence."""

    repo: int
    expected: dict[int, bool]
    flagged: dict[int, bool]

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.add_scope()
        self.expected = {}
        self.flagged = {}
        for cls in ("primary", "subagent", "reviewer", "other"):
            source = self.add_source(cls, session="evidence", cls=cls)
            for kind, flags, eligible in (
                ("prompt", 1, False),
                ("harness", 1, False),
                ("harness", 0, False),
                ("tool_error", 0, False),
                ("tool_call", 0, False),
                ("delegation", 0, False),
                ("prompt", 0, True),
                ("reply", 0, True),
            ):
                eid = self.add_event(
                    source,
                    self.repo,
                    f"evidence {cls} {kind} {flags}",
                    kind=kind,
                    flags=flags,
                )
                self.expected[eid] = eligible if cls == "primary" else False
                self.flagged[eid] = bool(flags)

    def test_open_and_neighbours_mark_answer_evidence(self) -> None:
        for eid, eligible in self.expected.items():
            with self.subTest(eid=eid):
                opened = query.open_event(
                    self.ro(), cast("str", eid), roots={}, context=20
                )
                self.assertIs(opened.get("answer_citable"), eligible)
                self.assertIn("navigation", opened["preview_notice"])
                for neighbour in opened["neighbours"]:
                    self.assertIs(
                        neighbour.get("answer_citable"),
                        self.expected[neighbour["id"]],
                    )
                    self.assertIs(
                        neighbour.get("flagged"), self.flagged[neighbour["id"]]
                    )

    def test_timeline_marks_all_rows_and_preserves_navigation(self) -> None:
        out = query.session(self.ro(), "evidence")
        self.assertEqual({r["id"] for r in out["events"]}, set(self.expected))
        for row in out["events"]:
            with self.subTest(eid=row["id"]):
                self.assertIs(
                    row.get("answer_citable"), self.expected[row["id"]]
                )
                opened = query.open_event(self.ro(), row["ref"], roots={})
                self.assertEqual(opened["id"], row["id"])
        self.assertIn("navigation", out["preview_notice"])

    def test_search_marks_explicit_nondefault_hits(self) -> None:
        out = self.search(
            "evidence",
            kinds=set(query.ALL_KINDS),
            include_subagents=True,
            session="evidence",
            limit=30,
        )
        self.assertEqual(len(out["hits"]), 12)
        for hit in out["hits"]:
            with self.subTest(eid=hit["id"]):
                self.assertIs(
                    hit.get("answer_citable"), self.expected[hit["id"]]
                )
        self.assertIn("navigation", out["preview_notice"])

    def test_session_preview_retains_first_prompt_flags(self) -> None:
        normal = self.add_source("normal")
        self.add_event(normal, self.repo, "Normal first prompt")
        empty = self.add_source("no-prompt")
        self.add_event(empty, self.repo, "Only a call", kind="tool_call")
        out = query.sessions(self.ro(), cwd="/repo")
        rows = {r["session"]: r for r in out["sessions"]}
        for root, flagged, eligible in (
            ("evidence", True, False),
            ("normal", False, True),
            ("no-prompt", False, False),
        ):
            with self.subTest(root=root):
                self.assertIs(rows[root].get("preview_flagged"), flagged)
                self.assertIs(
                    rows[root].get("preview_answer_citable"), eligible
                )
        self.assertEqual(
            rows["evidence"]["preview"], "evidence primary prompt 1"
        )
        self.assertIsNone(rows["no-prompt"]["preview"])
        self.assertIn("navigation", out["preview_notice"])


class NoticeTests(unittest.TestCase):
    """The data-not-instructions notice has one source of truth."""

    def test_notice_matches_the_classifier(self) -> None:
        self.assertEqual(query.NOTICE, classify.NOTICE)  # pasted copies flag


class QuoteCheckTests(QueryCase):
    """Quote lookup that ignores whitespace but not case."""

    def test_quote_check_whitespace_collapse(self) -> None:
        repo = self.add_scope("/repo")
        text = (
            "We decided:\n  use   the\tcache\n\n for lookups.  Done. use the"
        )
        src = self.add_source("thr-q")
        eid = self.add_event(src, repo, text)

        def check(quote: str, ref: str = "codex:thr-q:1.1") -> dict[str, Any]:
            """Run ``quote_check`` against the event under test.

            Args:
                quote: The text to look for.
                ref: The event id or REF to check against.

            Returns:
                The quote-check answer.
            """
            return query.quote_check(self.ro(), ref, quote)

        got = check("use the cache for lookups")
        self.assertEqual(set(got), {"match", "span"})
        self.assertTrue(got["match"])
        start, end = got["span"]
        self.assertEqual(
            text[start:end], "use   the\tcache\n\n for lookups"
        )  # the original text, whitespace and all
        self.assertEqual(
            check("  use\nthe   cache  for lookups ")["span"], [start, end]
        )
        self.assertEqual(check("We decided: use the")["span"][0], 0)
        tail = check("Done. use the")["span"]
        self.assertEqual(tail[1], len(text))
        self.assertEqual(text[tail[0] : tail[1]], "Done. use the")
        first = check("use the")["span"]  # two occurrences: the first wins
        self.assertEqual(text[first[0] : first[1]], "use   the")
        self.assertEqual(
            check("cache for")["span"],
            [text.index("cache"), text.index("for lookups") + 3],
        )
        no = {"match": False, "span": None}
        for quote in ("USE the cache", "the cache for lookups!", "", "  \n "):
            with self.subTest(quote):
                self.assertEqual(check(quote), no)
        self.assertEqual(check("use the", ref=str(eid))["match"], True)
        self.assertEqual(check("use the", ref="codex:thr-q:1")["match"], True)
        self.assertEqual(
            check("use the", ref="codex:thr-q:9.1"),
            no | {"error": "not_found"},
        )
        self.assertEqual(check("x", ref="junk"), no | {"error": "bad_ref"})

    def test_quote_check_is_linear_on_a_large_event(self) -> None:
        repo = self.add_scope("/repo")
        text = "a " * 32000 + "needle  here"  # 64 KiB, 32,000 words
        self.add_event(self.add_source("thr-l"), repo, text)
        start = time.monotonic()
        got = query.quote_check(self.ro(), "codex:thr-l:1.1", "needle here")
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(text[got["span"][0] : got["span"][1]], "needle  here")
