"""Pull versus push of typed entries: add notes, search flags, loop scope."""

from __future__ import annotations

import time
from typing import Any

from muninn import knowledge, query
from muninn.knowledge_model import (
    PULL_EXPIRED,
    PULL_ONLY,
    PULL_RESTRICTED,
)
from muninn.knowledge_push import pull_only_note
from tests.hook_support import RecallCase
from tests.knowledge_support import kid

WORDS = "alphaterm betaterm gammaterm decision"


class PullOnlyNoteTests(RecallCase):
    """``know add`` says why an entry will not be pushed."""

    def test_each_reason_gets_its_own_accurate_note(self) -> None:
        plain = self.add(text=f"plain {WORDS}")
        # HookCase.add backs the text after the add call, so ask again.
        self.assertIsNone(pull_only_note(self.ro(), kid(plain)))
        private = self.add(text=f"private {WORDS}", sensitivity="restricted")
        self.assertEqual(private["pull_only"], PULL_RESTRICTED)
        self.assertNotIn("cite a user prompt", private["pull_only"])
        loose = self.add(text="unrelated words", backed=False)
        self.assertEqual(loose["pull_only"], PULL_ONLY)

    def test_an_expired_entry_gets_the_expiry_note(self) -> None:
        got = self.add(text=f"stale {WORDS}", valid_until="2099-01-01")
        self.rw.execute(
            "UPDATE knowledge SET valid_until = ?", (time.time() - 5,)
        )
        self.assertEqual(pull_only_note(self.ro(), kid(got)), PULL_EXPIRED)

    def test_restricted_wins_over_an_uncited_text(self) -> None:
        got = self.add(
            text="unrelated words", sensitivity="restricted", backed=False
        )
        self.assertEqual(got["pull_only"], PULL_RESTRICTED)


class SearchKnowledgeTests(RecallCase):
    """The CLI search shows restricted entries, flagged; hooks do not."""

    def setUp(self) -> None:
        super().setUp()
        self.plain = kid(self.add(text=f"plain {WORDS}"))
        self.secret = kid(
            self.add(text=f"secret {WORDS}", sensitivity="restricted")
        )
        stale = self.add(text=f"stale {WORDS}", valid_until="2099-01-01")
        self.rw.execute(
            "UPDATE knowledge SET valid_until = ? WHERE id = ?",
            (time.time() - 5, kid(stale)),
        )

    def find(
        self, *, all_projects: bool = False, push_only: bool = False
    ) -> dict[str, Any]:
        """Search the repo for the made-up terms."""
        return query.search(
            self.ro(),
            "alphaterm",
            cwd="/repo",
            env={},
            all_projects=all_projects,
            push_only=push_only,
        )

    def test_restricted_is_returned_flagged_and_expired_is_counted(
        self,
    ) -> None:
        for everywhere in (False, True):
            out = self.find(all_projects=everywhere)
            flags = {a["id"]: a["restricted"] for a in out["knowledge"]}
            self.assertEqual(
                flags, {f"K{self.plain}": False, f"K{self.secret}": True}
            )
            self.assertEqual(out["knowledge_expired_omitted"], 1)

    def test_a_hook_search_gets_pushable_entries_only(self) -> None:
        out = self.find(push_only=True)
        self.assertEqual(
            [a["id"] for a in out["knowledge"]], [f"K{self.plain}"]
        )
        self.assertNotIn("knowledge_expired_omitted", out)

    def test_restricted_entries_do_not_crowd_recall(self) -> None:
        for i in range(4):
            self.add(text=f"secret {WORDS} {i}", sensitivity="restricted")
        self.noise(self.repo)
        recalled = str(self.ask())
        self.assertIn(f"plain {WORDS}", recalled)
        self.assertNotIn("secret alphaterm", recalled)


class LoopScopeTests(RecallCase):
    """A loop entry is pull-only by scope: only an explicit ask finds it."""

    def setUp(self) -> None:
        super().setUp()
        self.loop = kid(self.add(text=f"looped {WORDS}", loop="run-9"))
        self.noise(self.repo)

    def test_not_pushed_at_session_start_or_recall(self) -> None:
        self.assertNotIn("looped", self.body(self.start()))
        self.assertNotIn("looped", str(self.ask()))

    def test_search_finds_it_only_with_all_projects(self) -> None:
        here = query.search(self.ro(), "alphaterm", cwd="/repo", env={})
        self.assertEqual(here["knowledge"], [])
        everywhere = query.search(
            self.ro(), "alphaterm", cwd="/repo", env={}, all_projects=True
        )
        self.assertEqual(
            [a["id"] for a in everywhere["knowledge"]], [f"K{self.loop}"]
        )
        self.assertEqual(
            knowledge.list_entries(self.ro(), cwd="/repo")["count"], 0
        )
