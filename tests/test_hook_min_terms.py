"""The recall term floor in ``prompt_terms`` and recall_block."""

from __future__ import annotations

from contextlib import closing
from typing import cast

from muninn import hook_recall, store
from tests.hook_support import RecallCase

TERMS = ["rareone", "raretwo", "commonone"]


class MinTermsTests(RecallCase):
    """The floor is MIN_TERMS terms."""

    def terms(self, prompt: str) -> tuple[object, dict[str, object]]:
        """Run ``prompt_terms``; return its result and the trace."""
        trace: dict[str, object] = {}
        return hook_recall.prompt_terms({"prompt": prompt}, trace), trace

    def stages(self) -> dict[str, int]:
        """Return the stage counts of a recall on the rare/common prompt."""
        request = hook_recall.RecallRequest(
            terms=TERMS,
            cwd=str(self.scope.project_id),
            notes=lambda: (),
            limit=4000,
        )
        payload = {"prompt": " ".join(TERMS), "session_id": "sess-now"}
        trace: dict[str, object] = {}
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            hook_recall.recall_block(conn, payload, self.env, trace, request)
        return cast("dict[str, int]", trace["stages"])

    def test_prompt_terms_floor(self) -> None:
        got, trace = self.terms("alphaterm betaterm")
        self.assertEqual((got, trace["skipped"]), (None, "short"))
        got, trace = self.terms("alphaterm betaterm gammaterm")
        self.assertEqual(len(cast("list[str]", got)), 3)
        self.assertEqual(trace, {"n_terms": 3})

    def test_two_term_events_are_dropped(self) -> None:
        self.talk("a", "rareone raretwo filler")
        self.noise(self.repo)
        got = self.stages()
        self.assertEqual((got["floor_dropped"], got["miss_floor"]), (1, 1))

    def test_mixed_events_give_an_exact_drop_count(self) -> None:
        self.talk("full", "rareone raretwo commonone")
        self.talk("two-a", "rareone raretwo filler")
        self.talk("two-b", "raretwo commonone filler")
        self.noise(self.repo)
        got = self.stages()
        self.assertEqual((got["floor_dropped"], got["returned"]), (2, 1))
