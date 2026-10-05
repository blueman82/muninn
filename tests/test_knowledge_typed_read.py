"""Typed field reads: derived expiry and restricted entries never pushed."""

from __future__ import annotations

import sqlite3
import time
from typing import Any
from unittest import mock

from muninn import hook, knowledge, obs_stats, query, store
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

    def test_list_refuses_a_bad_loop_id_like_add_does(self) -> None:
        for bad in ("bad id!! " * 20, "x" * 65, ""):
            got = knowledge.list_entries(self.ro(), cwd="/x", loop=bad)
            self.assertEqual(got["error"], "bad_loop_scope", bad)
        ok = knowledge.list_entries(self.ro(), cwd="/x", loop="never-seen")
        self.assertEqual(ok["count"], 0)
        self.assertIs(ok["loop_not_found"], True)

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

    def test_restricted_count_ignores_retracted_and_erased(self) -> None:
        self.add(text="live private", sensitivity="restricted")
        for state in ("retracted", "erased"):
            gone = self.add(text=f"{state} private", sensitivity="restricted")
            self.rw.execute(
                "UPDATE knowledge SET status = ? WHERE id = ?",
                (state, kid(gone)),
            )
        out = obs_stats.stats(self.ro(), self.home, {})
        self.assertEqual(out["knowledge_restricted"], 1)


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

    def test_search_flags_restricted_and_leaves_out_expired(self) -> None:
        """The CLI is a pull path: restricted is shown, flagged."""
        secret = self.add(
            text="secretword restricted", sensitivity="restricted"
        )
        stale = self.add(text="secretword stale", valid_until="2099-01-01")
        self.rw.execute(
            "UPDATE knowledge SET valid_until = ? WHERE id = ?",
            (time.time() - 5, kid(stale)),
        )
        plain = self.add(text="secretword plain")
        got = hits.knowledge(self.ro(), "secretword", [], True)
        self.assertEqual(
            {a["id"]: a["restricted"] for a in got},
            {f"K{kid(plain)}": False, f"K{kid(secret)}": True},
        )
        pushed = hits.knowledge(
            self.ro(), "secretword", [], True, push_only=True
        )
        self.assertEqual([a["id"] for a in pushed], [f"K{kid(plain)}"])

    def test_session_start_trace_counts_what_it_withheld(self) -> None:
        self.add(text="use the zebra cache")
        self.add(text="restricted one", sensitivity="restricted")
        stale = self.add(text="stale one", valid_until="2099-01-01")
        self.rw.execute(
            "UPDATE knowledge SET valid_until = ? WHERE id = ?",
            (time.time() - 5, kid(stale)),
        )
        trace: dict[str, object] = {}
        hook.session_start(self.payload(), "claude", self.env, trace=trace)
        self.assertEqual(trace["withheld"], {"expired": 1, "restricted": 1})

    def test_a_store_fault_in_withheld_keeps_the_block(self) -> None:
        self.add(text="use the zebra cache")
        for fault in (
            sqlite3.OperationalError("disk I/O error"),
            store.StoreUnavailableError("locked"),
            store.HotJournalError("hot"),
        ):
            trace: dict[str, object] = {}
            with (
                self.subTest(fault=type(fault).__name__),
                mock.patch.object(knowledge, "withheld", side_effect=fault),
            ):
                out = hook.session_start(
                    self.payload(), "claude", self.env, trace=trace
                )
            self.assertNotIn("error", trace)
            self.assertNotIn("withheld", trace)
            self.assertIn("zebra", str(out["hookSpecificOutput"]))

    def test_a_bug_in_withheld_reaches_the_outer_handler(self) -> None:
        self.add(text="use the zebra cache")
        trace: dict[str, object] = {}
        with mock.patch.object(
            knowledge, "withheld", side_effect=RuntimeError("boom")
        ):
            out = hook.session_start(
                self.payload(), "claude", self.env, trace=trace
            )
        self.assertEqual(trace["error"], "error")
        self.assertNotIn("zebra", str(out["hookSpecificOutput"]))

    def test_restricted_is_still_visible_through_list_and_show(self) -> None:
        got = self.add(text="private note", sensitivity="restricted")
        ro = self.ro()
        listed = knowledge.list_entries(ro, cwd="/repo")
        self.assertEqual(listed["count"], 1)
        shown = knowledge.show(ro, kid(got))
        self.assertEqual(shown["entry"]["sensitivity"], "restricted")
