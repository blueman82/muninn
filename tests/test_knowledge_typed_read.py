"""Typed field reads: derived expiry and restricted entries never pushed."""

from __future__ import annotations

import time
from typing import Any

from muninn import knowledge, obs_stats, query
from muninn.query import hits
from tests.hook_support import RecallCase
from tests.knowledge_support import KnowCase, kid

CITE = "use the zebra cache"


class ExpiryTests(KnowCase):
    """An entry past valid_until is reported expired, never deleted."""

    def expire(self, entry: dict[str, Any]) -> int:
        """Move an entry's expiry into the past, as time passing would."""
        number = kid(entry)
        self.rw.execute(
            "UPDATE knowledge SET valid_until = ? WHERE id = ?",
            (time.time() - 5, number),
        )
        return number

    def test_expired_is_flagged_not_current_and_not_deleted(self) -> None:
        live = kid(self.add(text="still valid", valid_until="2099-01-01"))
        old = self.expire(
            self.add(text="goes stale", valid_until="2099-01-01")
        )
        ro = self.ro()
        current = knowledge.list_entries(ro, cwd="/repo")
        self.assertEqual([e["id"] for e in current["entries"]], [f"K{live}"])
        stale = knowledge.list_entries(ro, cwd="/repo", status="expired")
        self.assertEqual(
            [(e["id"], e["expired"], e["status"]) for e in stale["entries"]],
            [(f"K{old}", True, "current")],
        )
        every = knowledge.list_entries(ro, cwd="/repo", status="all")
        self.assertEqual(every["count"], 2)
        shown = knowledge.show(ro, old)
        self.assertIs(shown["entry"]["expired"], True)
        self.assertEqual(shown["entry"]["status"], "current")
        row = self.rw.execute(
            "SELECT status FROM knowledge WHERE id = ?", (old,)
        ).fetchone()
        self.assertEqual(row[0], "current")  # status was never rewritten
        self.assertEqual(knowledge.check(ro)["expired"], 1)

    def test_expired_is_not_pushed_or_searched(self) -> None:
        got = self.add(text="use the zebra cache")
        self.assertEqual(
            len(knowledge.block_entries(self.ro(), [self.repo])), 1
        )
        self.expire(got)
        ro = self.ro()
        self.assertEqual(knowledge.block_entries(ro, [self.repo]), [])
        self.assertEqual(knowledge.user_cited(ro, [kid(got)]), set())
        found = query.search(ro, "zebra", cwd="/repo", env={})
        self.assertEqual(found["knowledge"], [])

    def test_stats_counts_expired_and_restricted_without_text(self) -> None:
        self.add(text="fine entry")
        self.expire(self.add(text="stale entry"))
        self.add(text="private entry", sensitivity="restricted")
        out = obs_stats.stats(self.ro(), self.home, {})
        self.assertEqual(out["knowledge"], {"current": 2, "expired": 1})
        self.assertEqual(out["knowledge_restricted"], 1)
        self.assertNotIn("private entry", str(out))


class RestrictedTests(RecallCase):
    """Restricted entries stay in the CLI views and out of every push."""

    def test_restricted_never_in_session_block_or_recall(self) -> None:
        got = self.add(
            text="restricted alphaterm betaterm gammaterm decision",
            sensitivity="restricted",
        )
        plain = self.add(text="plain alphaterm betaterm gammaterm decision")
        self.noise(self.repo)
        ro = self.ro()
        ids = [e["id"] for e in knowledge.block_entries(ro, [self.repo])]
        self.assertEqual(ids, [f"K{kid(plain)}"])
        self.assertEqual(
            knowledge.user_cited(ro, [kid(got), kid(plain)]), {kid(plain)}
        )
        start = str(self.start())
        self.assertNotIn(f"K{kid(got)}", start)
        self.assertIn(f"K{kid(plain)}", start)
        recalled = str(self.ask())
        self.assertNotIn("restricted alphaterm", recalled)
        self.assertIn("plain alphaterm", recalled)

    def test_search_leaves_out_restricted_and_expired(self) -> None:
        """Search output reaches the model, so it is a push path too."""
        self.add(text="secretword restricted", sensitivity="restricted")
        stale = self.add(text="secretword stale", valid_until="2099-01-01")
        self.rw.execute(
            "UPDATE knowledge SET valid_until = ? WHERE id = ?",
            (time.time() - 5, kid(stale)),
        )
        plain = self.add(text="secretword plain")
        got = hits.knowledge(self.ro(), "secretword", [], True)
        self.assertEqual([a["id"] for a in got], [f"K{kid(plain)}"])

    def test_restricted_is_still_visible_through_list_and_show(self) -> None:
        got = self.add(text="private note", sensitivity="restricted")
        ro = self.ro()
        listed = knowledge.list_entries(ro, cwd="/repo")
        self.assertEqual(listed["count"], 1)
        shown = knowledge.show(ro, kid(got))
        self.assertEqual(shown["entry"]["sensitivity"], "restricted")
