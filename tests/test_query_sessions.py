"""Query contract: the session listing and one session timeline."""

from __future__ import annotations

import time
from typing import Any

from pctx import query
from tests.query_support import NOTICE, QueryCase


class SessionsTests(QueryCase):
    """Session listing filters and the per-session event timeline."""

    repo: int
    beta: int
    a_main: int
    a_fork: int
    a_sub: int
    a_rev: int
    a1: int
    a2: int
    a3: int
    af: int
    asub: int
    b0: int
    b1: int
    b2: int

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.add_scope("/repo")
        self.beta = self.add_scope("/proj/beta")
        a = {"session": "aaaa-root"}
        self.a_main = self.add_source("a-main", **a)
        self.a_fork = self.add_source("a-fork", forked="a-main", **a)
        self.a_sub = self.add_source("a-sub", cls="subagent", **a)
        self.a_rev = self.add_source("a-rev", cls="reviewer", **a)

        def ev(src: int, text: str, ts: str, **kw: Any) -> int:
            """Add a dated repo event to a source.

            Args:
                src: The source id.
                text: The event text.
                ts: The event timestamp.
                **kw: Event overrides, as for ``add_event``.

            Returns:
                The event id.
            """
            return self.add_event(src, self.repo, text, ts=ts, **kw)

        self.a1 = ev(
            self.a_main, "  First   prompt\nof A ", "2026-09-01T10:00:00Z"
        )
        self.a2 = ev(
            self.a_main, "reply", "2026-09-01T10:01:00Z", kind="reply"
        )
        self.a3 = ev(
            self.a_main, "call", "2026-09-01T10:02:00Z", kind="tool_call"
        )
        self.af = ev(self.a_fork, "fork prompt", "2026-09-01T10:01:30Z")
        self.asub = ev(
            self.a_sub, "do it", "2026-09-01T10:01:15Z", kind="delegation"
        )
        b = self.add_source("b-main", session="bbbb-root", provider="claude")
        self.b0 = ev(
            b, "<env>", "2026-09-15T09:00:00Z", kind="harness", tag="env"
        )
        self.b1 = ev(b, "B " + "long " * 60, "2026-09-15T09:01:00Z")
        self.b2 = ev(b, "b reply", "2026-09-16T12:00:00Z", kind="reply")
        d = self.add_source("d-main", session="dddd-root", status="missing")
        ev(d, "old prompt", "2026-08-01T08:00:00Z")
        c = self.add_source("c-main", session="cccc-root")
        self.add_event(c, self.beta, "beta prompt", ts="2026-09-20T08:00:00Z")

    def roots(self, result: dict[str, Any]) -> list[str]:
        """Return the session roots of a listing in order.

        Args:
            result: A ``query.sessions`` answer.

        Returns:
            One session root per listed session.
        """
        return [s["session"] for s in result["sessions"]]

    def sessions(self, **kw: Any) -> dict[str, Any]:
        """List the sessions visible from the repo.

        Args:
            **kw: Extra ``query.sessions`` keywords.

        Returns:
            The listing answer.
        """
        return query.sessions(self.ro(), cwd="/repo", **kw)

    def test_sessions_and_session_timeline_order(self) -> None:
        got = self.sessions()
        self.assertEqual(got["notice"], NOTICE)
        self.assertEqual(
            self.roots(got), ["bbbb-root", "aaaa-root", "dddd-root"]
        )
        by_root = {s["session"]: s for s in got["sessions"]}
        a = by_root["aaaa-root"]
        self.assertEqual(
            a,
            {
                "session": "aaaa-root",
                "provider": "codex",
                "first_ts": "2026-09-01T10:00:00Z",
                "last_ts": "2026-09-01T10:02:00Z",
                "events": 4,  # primary threads only: 3 + the fork's 1
                "kinds": {"prompt": 2, "reply": 1, "tool_call": 1},
                "threads": 4,  # main, fork, subagent, reviewer
                "forks": 1,
                "preview": "First prompt of A",
                "preview_flagged": False,
                "preview_answer_citable": True,
                "status": "active",
                "scope": "repo",
            },
        )
        b = by_root["bbbb-root"]
        self.assertEqual(b["provider"], "claude")
        self.assertEqual(b["kinds"], {"harness": 1, "prompt": 1, "reply": 1})
        self.assertEqual(b["first_ts"], "2026-09-15T09:00:00Z")  # the harness
        self.assertEqual(len(b["preview"]), 120)
        self.assertTrue(b["preview"].startswith("B long long"))
        self.assertEqual(by_root["dddd-root"]["status"], "missing")
        timeline = query.session(self.ro(), "aaaa-root")["events"]
        self.assertEqual(
            [e["id"] for e in timeline],
            [self.a1, self.a2, self.asub, self.af, self.a3],
        )

    def test_sessions_filters_and_limit(self) -> None:
        self.assertEqual(
            self.roots(self.sessions(since="2026-09-10")), ["bbbb-root"]
        )
        wide = self.sessions(all_projects=True)
        self.assertEqual(self.roots(wide)[0], "cccc-root")
        self.assertEqual(wide["sessions"][0]["scope"], "beta")
        one = self.sessions(limit=1)
        self.assertEqual(self.roots(one), ["bbbb-root"])
        self.assertTrue(one["has_more"])
        self.assertFalse(self.sessions()["has_more"])
        nowhere = query.sessions(self.ro(), cwd="/nowhere")
        self.assertEqual(nowhere["sessions"], [])
        bad = self.sessions(since="yesterday")
        self.assertEqual(bad, {"error": "bad_date", "notice": NOTICE})
        stale = self.sessions(status={"last_pass_at": time.time() - 900})
        self.assertEqual(stale["poller"], "stale")

    def test_session_timeline_order_and_paging(self) -> None:
        every = query.session(self.ro(), "aaaa-root")
        self.assertEqual(every["notice"], NOTICE)
        self.assertEqual((every["provider"], every["total"]), ("codex", 5))
        order = [e["id"] for e in every["events"]]
        # ts order across threads: main, main, subagent, fork, main
        self.assertEqual(
            order, [self.a1, self.a2, self.asub, self.af, self.a3]
        )
        sub = every["events"][2]
        self.assertEqual(sub["class"], "subagent")
        self.assertEqual(sub["kind"], "delegation")
        first = every["events"][0]
        self.assertEqual(
            first,
            {
                "id": self.a1,
                "ref": "codex:a-main:1.1",
                "ts": "2026-09-01T10:00:00Z",
                "role": "user",
                "kind": "prompt",
                "tag": None,
                "preview": "First prompt of A",
                "answer_citable": True,
            },
        )
        self.assertIsNone(every["next_from"])
        seen, cursor = [], None
        while True:
            page = query.session(self.ro(), "aaaa", from_id=cursor, limit=2)
            seen += [e["id"] for e in page["events"]]
            cursor = page["next_from"]
            if cursor is None:
                break
        self.assertEqual(seen, order)

    def test_session_rows_are_short_flagged_and_can_start_at_an_undated_event(
        self,
    ) -> None:
        z = self.add_source("z-main", session="zzzz-root")
        undated = self.add_event(z, self.repo, "no timestamp " * 30)
        flagged = self.add_event(
            z, self.repo, "pasted block", ts="2026-09-02T00:00:00Z", flags=1
        )
        got = query.session(self.ro(), "zzzz-root")
        first = got["events"][0]
        self.assertEqual(len(first["preview"]), 80)
        self.assertNotIn("flagged", first)
        self.assertTrue(got["events"][1]["flagged"])
        resumed = query.session(self.ro(), "zzzz-root", from_id=undated)
        self.assertEqual(
            [e["id"] for e in resumed["events"]], [undated, flagged]
        )
        later = query.session(self.ro(), "zzzz-root", from_id=flagged)
        self.assertEqual([e["id"] for e in later["events"]], [flagged])

    def test_an_exact_session_root_beats_longer_roots_it_prefixes(
        self,
    ) -> None:
        self.add_source("s-short", session="abcd")
        other = self.add_source("s-long", session="abcdef")
        self.add_event(other, self.repo, "belongs to the long one")
        got = query.session(self.ro(), "abcd")
        self.assertEqual((got["session"], got["total"]), ("abcd", 0))
        self.assertEqual(
            query.session(self.ro(), "abcde")["session"], "abcdef"
        )
        got = query.search(
            self.ro(), "belongs", cwd="/repo", env={}, session="abcd"
        )
        self.assertEqual(got["hits"], [])  # the exact root, not abcdef

    def test_session_ties_nulls_and_errors(self) -> None:
        s = self.add_source("z-main", session="zzzz-root")
        y = self.add_source("y-thread", session="zzzz-root")
        same = "2026-09-02T00:00:00Z"
        late = self.add_event(s, self.repo, "late", ts=same)
        early = self.add_event(y, self.repo, "tie, earlier thread", ts=same)
        undated = self.add_event(s, self.repo, "no timestamp")
        got = query.session(self.ro(), "zzzz-root")
        self.assertEqual(
            [e["id"] for e in got["events"]], [undated, early, late]
        )
        self.add_source("zzzz-other", session="zzzz-2")
        errors = {
            "": "unknown_session",
            "nope": "unknown_session",
            "zzzz": "ambiguous_session",
        }
        for root, code in errors.items():
            with self.subTest(root):
                self.assertEqual(
                    query.session(self.ro(), root),
                    {"error": code, "notice": NOTICE},
                )
        got = query.session(self.ro(), "aaaa-root", from_id=undated)
        self.assertEqual(got, {"error": "bad_from", "notice": NOTICE})
