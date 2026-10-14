"""Query contract: search output is stable across the candidate extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.query_support import QueryCase

GOLDEN = Path(__file__).parent / "fixtures" / "query" / "search_golden.json"


class CandidateRowsTests(QueryCase):
    """``search`` answers match output captured before the extraction."""

    def build(self) -> list[dict[str, Any]]:
        """Fill a synthetic store and run the searches the golden covers.

        Returns:
            One search answer per case, in a fixed order.
        """
        repo = self.add_scope("/repo")
        other = self.add_scope("/other")
        for i, text in enumerate(
            ["apple pie", "apple apple tart", "banana bread", "apple banana"]
        ):
            self.add_event(self.add_source(f"t{i}"), repo, text)
        self.add_event(self.add_source("far"), other, "apple elsewhere")
        self.add_event(
            self.add_source("sub", cls="subagent"), repo, "apple subagent"
        )
        self.add_event(self.add_source("fl"), repo, "apple flag", flags=1)
        self.add_event(
            self.add_source("cur"), repo, "apple current", kind="reply"
        )
        self.noise(repo)
        self.env = {"CODEX_THREAD_ID": "cur"}
        cases: list[dict[str, Any]] = [
            {},
            {"all_projects": True},
            {"cwd": "/other"},
            {"kinds": {"reply"}, "include_current": True},
            {"include_current": True, "include_subagents": True},
            {"current_session": "t1", "limit": 2, "page": 2},
        ]
        return [self.search("apple banana", **case) for case in cases]

    def test_search_output_matches_golden(self) -> None:
        got = json.loads(json.dumps(self.build(), sort_keys=True))
        self.assertEqual(got, json.loads(GOLDEN.read_text()))


class PerSessionCapTests(QueryCase):
    """The per-session cap is a knob whose default is today's value."""

    def test_default_is_two_and_a_cap_changes_the_list(self) -> None:
        repo = self.add_scope("/repo")
        src = self.add_source("one")
        for text in ("kiwi alpha", "kiwi beta", "kiwi gamma"):
            self.add_event(src, repo, text)
        self.noise(repo)

        def count(**kw: Any) -> int:
            """Return how many hits the search keeps."""
            return len(self.search("kiwi", **kw)["hits"])

        self.assertEqual(count(), 2)
        self.assertEqual(count(per_session_cap=2), 2)
        self.assertEqual(count(per_session_cap=1), 1)
        self.assertEqual(count(per_session_cap=3), 3)
