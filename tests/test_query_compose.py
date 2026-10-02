"""Query contract: search composition, paging and context sections."""

from __future__ import annotations

import json
import time
from typing import Any

from muninn import scope
from tests.query_support import NOTICE, QueryCase


class SearchComposeTests(QueryCase):
    """Per-session caps, collapsed repeats and page composition."""

    repo: int

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.add_scope("/repo")

    def one(
        self, name: str, text: str, scope_id: int | None = None, **kw: Any
    ) -> int:
        """Add one event in its own source.

        Args:
            name: The thread id of the new source.
            text: The event text.
            scope_id: The scope; defaults to the repo scope.
            **kw: Event overrides, as for ``add_event``.

        Returns:
            The event id.
        """
        src = self.add_source(name)
        return self.add_event(src, scope_id or self.repo, text, **kw)

    def ids(self, result: dict[str, Any]) -> list[int]:
        """Return the hit ids in rank order.

        Args:
            result: A search answer.

        Returns:
            One event id per hit.
        """
        return [h["id"] for h in result["hits"]]

    def test_per_session_cap_2_and_explicit_tool_calls(self) -> None:
        crowd = self.add_source("crowd")
        for i in range(5):
            self.add_event(crowd, self.repo, f"zebra note {i}")
        for i in range(8):
            self.one(f"tc{i}", f"zebra command {i}", kind="tool_call")
        for i in range(3):
            self.one(f"p{i}", f"zebra prompt {i}")
        self.noise(self.repo)
        hits = self.search("zebra", limit=12)["hits"]
        crowded = [h for h in hits if h["session"] == "crowd"]
        self.assertEqual(len(crowded), 2)
        self.assertEqual(sum(h["kind"] == "tool_call" for h in hits), 0)
        self.assertEqual(len(hits), 2 + 3)
        every = self.search("zebra", kinds={"tool_call"}, limit=12)["hits"]
        self.assertEqual(len(every), 8)  # explicit tool calls remain available
        self.assertEqual(
            self.search("zebra", kinds={"prompt"})["hits"][0]["kind"], "prompt"
        )

    def test_identical_text_collapsed_with_repeats(self) -> None:
        rep = self.add_source("rep")
        for _ in range(3):
            self.add_event(rep, self.repo, "continue with zebra please")
        self.add_event(rep, self.repo, "zebra another text")
        self.one("o", "continue with zebra please")  # other session: apart
        self.noise(self.repo)
        hits = self.search("zebra")["hits"]
        same = [h for h in hits if "continue" in h["snippet"]]
        self.assertEqual(
            sorted((h["session"], h["repeats"]) for h in same),
            [("o", 0), ("rep", 2)],
        )

    def test_more_in_session_and_session_drilldown(self) -> None:
        big = self.add_source("big", session="bigroot")
        for i in range(5):
            self.add_event(big, self.repo, f"zebra entry {i}")
        self.one("small", "zebra lone entry")
        self.noise(self.repo)
        got = self.search("zebra")
        capped = [h for h in got["hits"] if h["session"] == "bigroot"]
        self.assertEqual([h["more_in_session"] for h in capped], [3, 3])
        lone = [h for h in got["hits"] if h["session"] == "small"]
        self.assertNotIn("more_in_session", lone[0])
        drill = self.search("zebra", session="bigroot")
        self.assertEqual(len(drill["hits"]), 5)  # all matches, no cap
        self.assertEqual({h["session"] for h in drill["hits"]}, {"bigroot"})
        self.assertNotIn("more_in_session", drill["hits"][0])
        self.assertEqual(
            self.search("zebra", session="bigr")["hits"], drill["hits"]
        )
        self.assertEqual(
            self.search("zebra", session="nope"),
            {"error": "unknown_session", "notice": NOTICE},
        )

    def test_more_in_session_excludes_collapsed_repeats(self) -> None:
        s = self.add_source("dup", session="duproot")
        for text in ("zebra a", "zebra a", "zebra b", "zebra c", "zebra d"):
            self.add_event(s, self.repo, text)
        self.noise(self.repo)
        hits = self.search("zebra")["hits"]
        self.assertEqual([h["repeats"] for h in hits], [1, 0])
        self.assertEqual([h["more_in_session"] for h in hits], [2, 2])

    def test_explicit_tool_calls_are_not_capped_per_page(self) -> None:
        # ranks: 4 tools + 1 prompt, then 5 tools, then 1 prompt
        plan = "TTTTP" + "TTTTTP"
        for rank, kind in enumerate(plan):
            self.one(
                f"r{rank}", f"zebra {'pad ' * rank}",
                kind="tool_call" if kind == "T" else "prompt",
            )  # fmt: skip
        self.noise(self.repo)
        requested = {"prompt", "tool_call"}
        first = self.search("zebra", limit=5, kinds=requested)["hits"]
        second = self.search("zebra", limit=5, page=2, kinds=requested)["hits"]
        third = self.search("zebra", limit=5, page=3, kinds=requested)["hits"]

        def kinds(hits: list[dict[str, Any]]) -> list[str]:
            """Return the kind of every hit.

            Args:
                hits: Hits of one result page.

            Returns:
                One kind per hit, in order.
            """
            return [h["kind"] for h in hits]

        self.assertEqual(kinds(first).count("tool_call"), 4)
        self.assertEqual(kinds(second).count("tool_call"), 5)
        self.assertEqual(kinds(third), ["prompt"])

    def test_more_in_session_only_when_hits_are_hidden(self) -> None:
        two = self.add_source("two")
        for i in range(2):
            self.add_event(two, self.repo, f"zebra pair {i}")
        one = self.add_source("one")  # 1 shown hit, tool calls held back
        self.add_event(one, self.repo, "zebra lead")
        for i in range(4):
            self.one(f"t{i}", f"zebra tool {i}", kind="tool_call")
        for i in range(3):
            self.add_event(one, self.repo, f"zebra call {i}", kind="tool_call")
        self.noise(self.repo)
        got = self.search("zebra", limit=30)
        by_session: dict[str, list[dict[str, Any]]] = {}
        for hit in got["hits"]:
            by_session.setdefault(hit["session"], []).append(hit)
        self.assertEqual(len(by_session["two"]), 2)
        for hit in by_session["two"] + by_session["one"]:
            self.assertNotIn("more_in_session", hit)

    def test_has_more_is_false_on_an_exact_last_page(self) -> None:
        for i in range(20):
            self.one(f"s{i}", f"zebra {'pad ' * i}")
        self.noise(self.repo)
        pages = [self.search("zebra", limit=10, page=n) for n in (1, 2)]
        self.assertEqual([p["has_more"] for p in pages], [True, False])

    def test_search_pages_are_disjoint(self) -> None:
        for i in range(25):
            self.one(f"s{i}", f"zebra {'pad ' * i}end")
        self.noise(self.repo)
        pages = [self.search("zebra", limit=10, page=n) for n in (1, 2, 3, 4)]
        self.assertEqual([len(p["hits"]) for p in pages], [10, 10, 5, 0])
        self.assertEqual(
            [p["has_more"] for p in pages], [True, True, False, False]
        )
        seen = [i for p in pages for i in self.ids(p)]
        self.assertEqual(len(seen), 25)
        self.assertEqual(len(set(seen)), 25)
        self.assertNotIn("note", pages[3])  # past the end is not "no matches"

    def test_recent_resorts_top_50_by_ts(self) -> None:
        for i in range(60):  # a longer text ranks lower, and is newer
            self.one(
                f"r{i}", f"zebra {'pad ' * i}", ts=f"2026-09-01T00:{i:02d}:00Z"
            )
        self.noise(self.repo)
        ranked = self.search("zebra", limit=30)["hits"]
        self.assertEqual(ranked[0]["ts"], "2026-09-01T00:00:00Z")
        got = self.search("zebra", recent=True, limit=30)["hits"]
        stamps = [h["ts"] for h in got]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(stamps[0], "2026-09-01T00:49:00Z")  # not :59

    def test_output_bounded_6kb(self) -> None:
        for i in range(40):
            text = f"zebra {'wordy filler text ' * 30}{i}"
            self.one(f"b{i}", text, cwd="/repo/some/deeply/nested/dir")
        self.noise(self.repo)
        default = self.search("zebra")
        self.assertLessEqual(len(json.dumps(default)), 6144)
        big = self.search("zebra", limit=30)
        self.assertLessEqual(len(json.dumps(big)), 6144)
        self.assertLess(len(big["hits"]), 30)
        self.assertNotIn("omitted", big)
        self.assertTrue(big["has_more"])


class SearchAroundTests(QueryCase):
    """Knowledge, other-scope counts, the zero-hit note, freshness."""

    repo: int
    beta: int

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.add_scope("/repo")
        self.beta = self.add_scope("/proj/beta")

    def know(
        self,
        text: str,
        scope_id: int | None = None,
        status: str = "current",
        **kw: Any,
    ) -> int:
        """Insert one knowledge entry.

        Args:
            text: The entry text.
            scope_id: The scope; defaults to the repo scope.
            status: The entry status, such as ``current``.
            **kw: Overrides for ``kind``, ``actor`` and ``at``.

        Returns:
            The knowledge id.
        """
        row = {"kind": "decision", "actor": "claude:abc", "at": 1790000000.0}
        row |= kw
        cursor = self.rw.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                scope_id or self.repo,
                row["kind"],
                text,
                status,
                row["actor"],
                row["at"],
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def test_knowledge_hits_first(self) -> None:
        wide = scope.global_scope_id(self.rw)
        k1 = self.know("use zebra caching for lookups")
        self.rw.execute(
            "INSERT INTO citation(knowledge_id, provider, thread_id, line,"
            " part, line_sha256, role, kind) VALUES (?, 'codex', 'thr', 12,"
            " 1, 'h', 'user', 'prompt')",
            (k1,),
        )
        k2 = self.know("global zebra preference", scope_id=wide, kind="fact")
        self.know("old zebra decision", status="superseded")
        self.know("retracted zebra", status="retracted")
        self.know("beta zebra decision", scope_id=self.beta)
        self.know("unrelated apples")
        self.add_event(self.add_source("a"), self.repo, "an event about zebra")
        self.noise(self.repo)
        got = self.search("zebra")
        self.assertLess(list(got).index("knowledge"), list(got).index("hits"))
        entries = {e["id"]: e for e in got["knowledge"]}
        self.assertEqual(set(entries), {f"K{k1}", f"K{k2}"})
        first = entries[f"K{k1}"]
        self.assertEqual(first["kind"], "decision")
        self.assertEqual(first["scope"], "repo")
        self.assertEqual(first["actor"], "claude:abc")
        self.assertEqual(first["date"], "2026-09-21")
        self.assertEqual(first["text"], "use zebra caching for lookups")
        self.assertEqual(first["cites"], ["codex:thr:12.1"])
        self.assertEqual(entries[f"K{k2}"]["cites"], [])
        self.assertEqual(len(got["hits"]), 1)  # episodic memory stays apart
        self.assertEqual(self.search("zebra", page=2)["knowledge"], [])
        every = self.search("zebra", all_projects=True)["knowledge"]
        self.assertEqual(len(every), 3)  # top 3 of the 3 current ones

    def test_knowledge_top_3_and_no_drilldown_section(self) -> None:
        for i in range(5):
            self.know(f"zebra decision {i}")
        self.add_event(self.add_source("a"), self.repo, "zebra event")
        self.noise(self.repo)
        self.assertEqual(len(self.search("zebra")["knowledge"]), 3)
        drill = self.search("zebra", session="a")
        self.assertEqual(drill["knowledge"], [])

    def test_other_scopes_counts_no_text(self) -> None:
        self.add_event(self.add_source("a"), self.repo, "zebra home")
        gamma = self.add_scope("/proj/gamma")
        self.add_event(self.add_source("b1"), self.beta, "zebra BETASECRET1")
        self.add_event(self.add_source("b2"), self.beta, "zebra BETASECRET2")
        self.add_event(self.add_source("g"), gamma, "zebra GAMMASECRET")
        sub = self.add_source("sub", cls="subagent")
        self.add_event(sub, self.beta, "zebra SUBAGENTSECRET", kind="reply")
        self.noise(self.repo)
        got = self.search("zebra")
        self.assertEqual(got["other_scopes"], {"beta": 2, "gamma": 1})
        dump = json.dumps(got)
        for secret in ("BETASECRET", "GAMMASECRET", "SUBAGENTSECRET"):
            self.assertNotIn(secret, dump)
        self.assertNotIn("note", got)  # it has an in-scope hit
        wide = self.search("zebra", all_projects=True)
        self.assertEqual(wide["other_scopes"], {})

    def test_knowledge_text_is_cut_and_only_live_cites_show(self) -> None:
        k = self.know("zebra " + "long " * 100)
        for state, line in (
            ("erased", 1),
            ("live", 2),
            ("live", 3),
            ("live", 4),
        ):
            self.rw.execute(
                "INSERT INTO citation(knowledge_id, provider, thread_id,"
                " line, part, line_sha256, role, kind, state) VALUES"
                " (?, 'codex', 'thr', ?, 1, 'h', 'user', 'prompt', ?)",
                (k, line, state),
            )
        self.noise(self.repo)
        entry = self.search("zebra")["knowledge"][0]
        self.assertEqual(len(entry["text"]), 300)
        self.assertEqual(entry["cites"], ["codex:thr:2.1", "codex:thr:3.1"])

    def test_exact_cwd_mode_counts_other_cwds_as_outside(self) -> None:
        self.add_event(
            self.add_source("a"), self.repo, "zebra one", cwd="/repo"
        )
        self.add_event(
            self.add_source("b"), self.repo, "zebra two", cwd="/repo/sub"
        )
        self.add_event(
            self.add_source("c"), self.repo, "zebra three"
        )  # no cwd
        self.noise(self.repo)
        got = self.search("zebra", scope="/repo")
        self.assertEqual(got["other_scopes"], {"repo": 2})
        self.assertNotIn("note", got)  # something matched exactly
        got = self.search("zebra", scope="/elsewhere")
        self.assertEqual(got["hits"], [])
        self.assertIn("3 matches outside this scope", got["note"])

    def test_zero_in_scope_reports_outside_matches(self) -> None:
        self.add_event(self.add_source("b1"), self.beta, "zebra one")
        self.add_event(self.add_source("b2"), self.beta, "zebra two")
        self.noise(self.repo)
        got = self.search("zebra", cwd="/repo")
        self.assertEqual(got["hits"], [])
        self.assertEqual(got["other_scopes"], {"beta": 2})
        self.assertIn("0 matches in this scope", got["note"])
        self.assertIn("2 matches outside this scope", got["note"])
        self.assertIn("--all-projects", got["note"])
        nothing = self.search("qqqq")
        self.assertEqual(nothing["hits"], [])
        self.assertNotIn("--all-projects", nothing.get("note", ""))

    def test_freshness_fields_only_with_status(self) -> None:
        self.add_event(self.add_source("a"), self.repo, "zebra")
        self.noise(self.repo)
        bare = self.search("zebra")
        self.assertNotIn("poller", bare)
        self.assertNotIn("index_age_s", bare)
        now = time.time()
        fresh = self.search("zebra", status={"last_pass_at": now - 30})
        self.assertEqual(fresh["poller"], "ok")
        self.assertTrue(25 <= fresh["index_age_s"] <= 40)
        lagging = {"last_pass_at": now - 130, "interval_s": 60}
        self.assertEqual(self.search("zebra", status=lagging)["poller"], "ok")
        stale = {"last_pass_at": now - 500, "interval_s": 60}
        got = self.search("zebra", status=stale)
        self.assertEqual(got["poller"], "stale")
        self.assertGreaterEqual(got["index_age_s"], 499)
        got = self.search("zebra", status={})  # no heartbeat at all
        self.assertEqual((got["index_age_s"], got["poller"]), (None, "stale"))

    def test_stage_counts(self) -> None:
        crowd = self.add_source("crowd")
        for i in range(4):
            self.add_event(crowd, self.repo, f"zebra {i}")
        self.add_event(self.add_source("b"), self.beta, "zebra beta")
        self.noise(self.repo)
        stages = self.search("zebra")["stages"]
        self.assertEqual(
            stages,
            {
                "matches": 5,  # eligible, every scope
                "in_scope": 4,
                "candidates": 4,
                "session_capped": 2,
                "tool_capped": 0,
                "returned": 2,
            },
        )
