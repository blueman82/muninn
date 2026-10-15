"""The per-session cap knob of ``search``: validation, annotation, stages."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

from muninn.query import ranked
from muninn.query.hits import PER_SESSION, compose
from tests.query_support import QueryCase


class CapKnobTests(QueryCase):
    """Edges of ``per_session_cap`` around the default of two."""

    def sessions(self, sizes: dict[str, int], kind: str = "prompt") -> None:
        """Add ``sizes[name]`` distinct kiwi events to each session."""
        repo = self.add_scope("/repo")
        for name, size in sizes.items():
            src = self.add_source(name)
            for i in range(size):
                self.add_event(src, repo, f"kiwi {name} {i}", kind=kind)
        self.noise(repo)

    def more(self, **kw: Any) -> list[Any]:
        """Return each hit's ``more_in_session`` (None when absent)."""
        hits = self.search("kiwi", **kw)["hits"]
        return [h.get("more_in_session") for h in hits]

    def test_invalid_cap_is_an_error_answer(self) -> None:
        self.sessions({"one": 3})
        for cap in (0, -1):
            got = self.search("kiwi", per_session_cap=cap)
            self.assertEqual(got["error"], "per_session_cap must be >= 1")
            self.assertNotIn("hits", got)

    def test_none_means_the_default(self) -> None:
        self.sessions({"one": 3})
        kw: dict[str, Any] = {"per_session_cap": None}
        self.assertEqual(self.search("kiwi", **kw), self.search("kiwi"))

    def test_explicit_default_is_byte_identical(self) -> None:
        self.sessions({"one": 3, "two": 1})
        cases: list[dict[str, Any]] = [
            {},
            {"limit": 1, "page": 2},
            {"all_projects": True},
        ]
        for case in cases:
            plain = json.dumps(self.search("kiwi", **case), sort_keys=True)
            named = json.dumps(
                self.search("kiwi", per_session_cap=PER_SESSION, **case),
                sort_keys=True,
            )
            self.assertEqual(plain, named)

    def test_annotation_edges(self) -> None:
        self.sessions({"one": 3})
        self.assertEqual(self.more(per_session_cap=10), [None] * 3)
        self.assertEqual(self.more(per_session_cap=3), [None] * 3)
        self.assertEqual(self.more(per_session_cap=2), [1, 1])

    def test_only_the_session_over_the_cap_is_annotated(self) -> None:
        self.sessions({"one": 3, "two": 1})
        hits = self.search("kiwi")["hits"]
        marks = {h["session"]: h.get("more_in_session") for h in hits}
        self.assertEqual(marks, {"one": 1, "two": None})

    def test_repeats_collapse_before_a_cap_of_one(self) -> None:
        repo = self.add_scope("/repo")
        src = self.add_source("one")
        for text in ("kiwi same", "kiwi same", "kiwi same", "kiwi other"):
            self.add_event(src, repo, text)
        self.noise(repo)
        got = self.search("kiwi", per_session_cap=1)
        self.assertEqual([h["repeats"] for h in got["hits"]], [2])
        self.assertEqual(got["stages"]["session_capped"], 1)

    def test_tool_cap_is_independent_of_the_session_cap(self) -> None:
        # Search never leaves tools on for kinds that include tool_call, so
        # the tool cap is exercised through compose.
        rows: Any = [
            {"session_root": f"s{i}", "text": f"t{i}", "kind": "tool_call"}
            for i in range(6)
        ]
        _, session_capped, tool_capped = compose(
            rows, 10, per_session=True, tools=True, per_session_cap=5
        )
        self.assertEqual((session_capped, tool_capped), (0, 2))
        self.sessions({f"s{i}": 1 for i in range(6)}, kind="tool_call")
        got = self.search("kiwi", per_session_cap=5, kinds={"tool_call"})
        self.assertEqual(got["stages"]["tool_capped"], 0)
        self.assertEqual(len(got["hits"]), 6)

    def test_session_listing_never_annotates_from_an_unused_cap(self) -> None:
        self.sessions({"one": 4})
        real = ranked.candidate_rows

        def short(conn: Any, fts: Any, args: Any) -> Any:
            """Fetch only two candidates so some matches are uncovered."""
            return real(conn, fts, args, 2)

        with mock.patch.object(ranked, "candidate_rows", short):
            got = self.search("kiwi", session="one", per_session_cap=1)
        self.assertEqual(len(got["hits"]), 2)
        self.assertNotIn("more_in_session", got["hits"][0])
