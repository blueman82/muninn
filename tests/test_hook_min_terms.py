"""The recall term floor as a parameter: ``prompt_terms`` and recall_block."""

from __future__ import annotations

from contextlib import closing
from typing import cast
from unittest import mock

from muninn import hook_recall, store
from tests.hook_support import RecallCase

TERMS = ["rareone", "raretwo", "commonone"]


class MinTermsTests(RecallCase):
    """The floor defaults to MIN_TERMS, never raises, and moves the cut."""

    def terms(
        self, prompt: str, **kw: int
    ) -> tuple[object, dict[str, object]]:
        """Run ``prompt_terms``; return its result and the trace."""
        trace: dict[str, object] = {}
        return hook_recall.prompt_terms({"prompt": prompt}, trace, **kw), trace

    def recall(
        self,
        terms: list[str] | None = None,
        min_terms: int = hook_recall.MIN_TERMS,
    ) -> dict[str, object]:
        """Run ``recall_block`` on the rare/common prompt; return the trace."""
        request = hook_recall.RecallRequest(
            terms=terms or TERMS,
            cwd=str(self.scope.project_id),
            notes=lambda: (),
            limit=4000,
        )
        payload = {"prompt": " ".join(TERMS), "session_id": "sess-now"}
        trace: dict[str, object] = {}
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            hook_recall.recall_block(
                conn, payload, self.env, trace, request, min_terms
            )
        return trace

    def stages(self, min_terms: int = hook_recall.MIN_TERMS) -> dict[str, int]:
        """Return the stage counts of a recall."""
        trace = self.recall(min_terms=min_terms)
        return cast("dict[str, int]", trace["stages"])

    def test_prompt_terms_floor(self) -> None:
        got, trace = self.terms("alphaterm betaterm")
        self.assertEqual((got, trace["skipped"]), (None, "short"))
        got, trace = self.terms("alphaterm betaterm", min_terms=2)
        self.assertEqual(got, ['"alphaterm"', '"betaterm"'])
        self.assertEqual(trace, {"n_terms": 2})
        got, _ = self.terms("alphaterm betaterm gammaterm", min_terms=4)
        self.assertIsNone(got)
        got, _ = self.terms("alphaterm", min_terms=1)
        self.assertEqual(got, ['"alphaterm"'])

    def test_a_floor_below_one_acts_as_one(self) -> None:
        for floor in (0, -1):
            got, _ = self.terms("alphaterm", min_terms=floor)
            self.assertEqual(got, ['"alphaterm"'])
            got, trace = self.terms("the of", min_terms=floor)
            self.assertEqual((got, trace["skipped"]), (None, "short"))
        self.talk("a", "rareone raretwo commonone")
        self.noise(self.repo)
        for floor in (0, -1):
            kept = self.recall(terms=["nomatchterm"], min_terms=floor)
            self.assertEqual(kept["returned_ids"], [])

    def test_a_lower_floor_keeps_two_term_events(self) -> None:
        self.talk("a", "rareone raretwo filler")
        self.noise(self.repo)
        default = self.stages()
        self.assertEqual(
            (default["floor_dropped"], default["miss_floor"]), (1, 1)
        )
        low = self.stages(min_terms=2)
        self.assertEqual((low["floor_dropped"], low["returned"]), (0, 1))
        self.assertNotIn("miss_floor", low)

    def test_a_higher_floor_than_the_request_drops_everything(self) -> None:
        self.talk("a", "rareone raretwo commonone")
        self.noise(self.repo)
        high = self.stages(min_terms=4)
        self.assertEqual(
            (high["floor_dropped"], high["returned"], high["miss_floor"]),
            (1, 0, 1),
        )

    def test_mixed_events_give_an_exact_drop_count(self) -> None:
        self.talk("full", "rareone raretwo commonone")
        self.talk("two-a", "rareone raretwo filler")
        self.talk("two-b", "raretwo commonone filler")
        self.noise(self.repo)
        got = self.stages()
        self.assertEqual((got["floor_dropped"], got["returned"]), (2, 1))
        got = self.stages(min_terms=2)
        self.assertEqual((got["floor_dropped"], got["returned"]), (0, 3))

    def test_a_low_floor_still_stops_at_max_events(self) -> None:
        for i in range(8):
            self.talk(f"two{i}", "rareone raretwo filler")
        self.noise(self.repo)
        with mock.patch.object(
            hook_recall.query, "search", wraps=hook_recall.query.search
        ) as spy:
            got = self.stages(min_terms=2)
        self.assertEqual(got["returned"], hook_recall.MAX_EVENTS)
        self.assertEqual(spy.call_count, 1)
