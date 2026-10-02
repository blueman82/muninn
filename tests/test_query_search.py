"""Query contract: search matching, scoping and filters."""

from __future__ import annotations

from typing import Any

from muninn import scope
from tests.query_support import NOTICE, QueryCase


class SearchCoreTests(QueryCase):
    """Term matching, hit shape and scope of default search."""

    repo: int

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.add_scope("/repo")

    def one(self, name: str, text: str, **kw: Any) -> int:
        """Add one event in its own source.

        Args:
            name: The thread id of the new source.
            text: The event text.
            **kw: Event overrides, as for ``add_event``.

        Returns:
            The event id.
        """
        return self.add_event(self.add_source(name), self.repo, text, **kw)

    def ids(self, result: dict[str, Any]) -> list[int]:
        """Return the hit ids in rank order.

        Args:
            result: A search answer.

        Returns:
            One event id per hit.
        """
        return [h["id"] for h in result["hits"]]

    def test_or_query_matches_any_term(self) -> None:
        a = self.one("a", "apple pie")
        b = self.one("b", "banana bread")
        self.one("c", "cherry tart")
        d = self.one("d", "apple banana split")
        self.noise(self.repo)
        got = self.search("apple banana")
        self.assertEqual(got["notice"], NOTICE)
        self.assertEqual(self.ids(got)[0], d)  # both terms rank first
        self.assertEqual(set(self.ids(got)), {a, b, d})

    def test_query_syntax_is_neutralized(self) -> None:
        alpha = self.one("a", "alpha only here")
        beta = self.one("b", "beta only here")
        cafe = self.one("c", "naive cafe uber")
        self.noise(self.repo)
        hostile = [
            "alpha AND NOT beta",
            "alpha OR (beta",
            '"alpha',
            "alpha*",
            "a:b -alpha",
            "NEAR(alpha beta)",
            "alpha ^ beta {x} [y]",
            'alpha" beta"',
            "'; --",
            "\\ / ( ) \" ' * : - ~",
            "()",
            "",
        ]
        for text in hostile:
            with self.subTest(text):
                got = self.search(text)
                self.assertIsInstance(got["hits"], list)
                self.assertEqual(got["notice"], NOTICE)
        got = self.search("alpha AND NOT beta")
        self.assertEqual(set(self.ids(got)), {alpha, beta})
        self.assertEqual(self.ids(self.search("naïve CAFÉ über")), [cafe])
        self.assertEqual(self.search("()")["hits"], [])

    def test_identifier_phrase_parts(self) -> None:
        exact = self.one("a", "please edit hook_core.py today")
        self.one("b", "the hook needs a core py script")
        self.noise(self.repo)
        got = self.search("hook_core.py")
        self.assertEqual(self.ids(got)[0], exact)

    def test_hit_shape(self) -> None:
        ts = "2026-09-30T10:11:12.000Z"
        src = self.add_source("0123456789abcdef", session="fedcba9876543210")
        eid = self.add_event(
            src, self.repo, "the zebra crossed", ts=ts, cwd="/repo/sub",
            tag="Bash", kind="tool_call", role="assistant", line=7, part=2,
        )  # fmt: skip
        hit = self.search("zebra", kinds={"tool_call"})["hits"][0]
        self.assertEqual(
            hit,
            {
                "id": eid,
                "ref": "codex:0123456789abcdef:7.2",
                "ts": ts,
                "provider": "codex",
                "session": "fedcba98",
                "thread": "01234567",
                "kind": "tool_call",
                "role": "assistant",
                "tag": "Bash",
                "scope": "repo",
                "cwd": "/repo/sub",
                "source_status": "active",
                "snippet": "the «zebra» crossed",
                "answer_citable": False,
                "repeats": 0,
            },
        )

    def test_only_primary_unflagged_default_kinds_returned(self) -> None:
        def ev(name: str, text: str, cls: str = "primary", **kw: Any) -> int:
            """Add one event in its own source of the given class.

            Args:
                name: The thread id of the new source.
                text: The event text.
                cls: The thread class of the source.
                **kw: Event overrides, as for ``add_event``.

            Returns:
                The event id.
            """
            src = self.add_source(name, cls=cls)
            return self.add_event(src, self.repo, text, **kw)

        want = {
            ev("p1", "sentinel prompt"),
            ev("p2", "sentinel reply", kind="reply"),
        }
        call = ev("p3", "sentinel call", kind="tool_call")
        harness = ev("p4", "sentinel h", kind="harness")
        ev("p5", "sentinel x", kind="tool_error")
        ev("p6", "sentinel flagged", flags=1)
        ev("s1", "sentinel sub", cls="subagent", kind="delegation")
        ev("s2", "sentinel sub2", cls="subagent", kind="reply")
        ev("r1", "sentinel reviewer", cls="reviewer")
        ev("o1", "sentinel other", cls="other")
        self.noise(self.repo)
        self.assertEqual(set(self.ids(self.search("sentinel"))), want)
        self.assertEqual(
            self.ids(self.search("sentinel", kinds={"tool_call"})), [call]
        )
        only = self.search("sentinel", kinds={"harness"})
        self.assertEqual(self.ids(only), [harness])  # explicit, still primary

    def test_scope_spans_worktrees(self) -> None:
        repo = self.tmp / "wtrepo"
        (repo / "sub").mkdir(parents=True)
        self.git("init", "-q", cwd=repo)
        self.git("commit", "-q", "--allow-empty", "-m", "i", cwd=repo)
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "f", str(wt), cwd=repo)
        (wt / "sub").mkdir()
        sid = scope.scope_id(self.rw, str(repo))
        eid = self.add_event(self.add_source("w"), sid, "needle text")
        self.noise(sid)
        for cwd in (repo, wt, repo / "sub", wt / "sub"):
            with self.subTest(str(cwd)):
                self.assertEqual(
                    self.ids(self.search("needle", cwd=str(cwd))), [eid]
                )
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.search("needle", cwd=str(elsewhere))["hits"], [])

    def test_all_projects_opt_in_labels_scope(self) -> None:
        beta = self.add_scope("/proj/beta")
        ea = self.one("a", "shared zebra token")
        eb = self.add_event(self.add_source("b"), beta, "shared zebra token")
        self.noise(self.repo)
        default = self.search("zebra", cwd="/repo")
        self.assertEqual(self.ids(default), [ea])
        wide = self.search("zebra", cwd="/repo", all_projects=True)
        self.assertEqual(
            {h["id"]: h["scope"] for h in wide["hits"]},
            {ea: "repo", eb: "beta"},
        )


class SearchFilterTests(QueryCase):
    """Session exclusion, date and kind filters, and result limits."""

    repo: int

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.add_scope("/repo")

    def one(self, name: str, text: str, **kw: Any) -> int:
        """Add one event in its own source.

        Args:
            name: The thread id of the new source.
            text: The event text.
            **kw: Event overrides, as for ``add_event``.

        Returns:
            The event id.
        """
        return self.add_event(self.add_source(name), self.repo, text, **kw)

    def ids(self, result: dict[str, Any]) -> set[int]:
        """Return the hit ids, ignoring rank.

        Args:
            result: A search answer.

        Returns:
            The set of event ids.
        """
        return {h["id"] for h in result["hits"]}

    def test_current_session_excluded_via_claude_env(self) -> None:
        mine = self.one("cur", "canary echo")
        old = self.one("old", "canary echo")
        self.noise(self.repo)
        got = self.search("canary", env={"CLAUDE_CODE_SESSION_ID": "cur"})
        self.assertEqual(self.ids(got), {old})
        self.assertNotIn(mine, self.ids(got))

    def test_current_session_excluded_via_codex_env(self) -> None:
        root = self.add_source("root1")
        fork = self.add_source("thread2", session="root1")
        a = self.add_event(root, self.repo, "canary echo")
        b = self.add_event(fork, self.repo, "canary echo again")
        old = self.one("old", "canary echo")
        self.noise(self.repo)
        for env in (
            {"CODEX_SESSION_ID": "root1"},
            {"CODEX_THREAD_ID": "thread2"},
        ):
            with self.subTest(env):
                got = self.search("canary", env=env)
                self.assertEqual(self.ids(got), {old})
        self.assertEqual(self.ids(self.search("canary")), {a, b, old})

    def test_include_current_overrides(self) -> None:
        mine = self.one("cur", "canary echo")
        old = self.one("old", "canary echo")
        self.noise(self.repo)
        got = self.search(
            "canary",
            env={"CLAUDE_CODE_SESSION_ID": "cur"},
            include_current=True,
        )
        self.assertEqual(self.ids(got), {mine, old})

    def test_current_session_kw_overrides_env(self) -> None:
        a = self.one("aaa", "canary echo")
        b = self.one("bbb", "canary echo")
        self.noise(self.repo)
        env = {"CLAUDE_CODE_SESSION_ID": "aaa"}
        got = self.search("canary", env=env, current_session="bbb")
        self.assertEqual(self.ids(got), {a})  # bbb excluded, aaa allowed
        codex = self.add_source("thr", session="rooty")
        c = self.add_event(codex, self.repo, "canary echo")
        got = self.search("canary", env={}, current_session="thr")
        self.assertEqual(self.ids(got), {a, b})  # a thread id maps to rooty
        self.assertNotIn(c, self.ids(got))

    def test_snippet_centered_on_match(self) -> None:
        text = "alpha " * 60 + "needle " + "omega " * 60
        self.one("s", text)
        self.noise(self.repo)
        snippet = self.search("needle")["hits"][0]["snippet"]
        self.assertIn("«needle»", snippet)
        self.assertTrue(snippet.startswith("…") and snippet.endswith("…"))
        self.assertTrue(30 <= len(snippet.split()) <= 36)  # 32 tokens

    def test_filters_provider_since_until(self) -> None:
        c1 = self.one("c1", "zebra a", ts="2026-09-01T10:00:00.000Z")
        c2 = self.one("c2", "zebra b", ts="2026-09-15T10:00:00.000Z")
        cl = self.add_event(
            self.add_source("cl", provider="claude"),
            self.repo,
            "zebra c",
            ts="2026-09-30T10:00:00.000Z",
        )
        self.noise(self.repo)
        self.assertEqual(self.ids(self.search("zebra")), {c1, c2, cl})
        got = self.search("zebra", provider="claude")
        self.assertEqual(self.ids(got), {cl})
        got = self.search("zebra", since="2026-09-10")
        self.assertEqual(self.ids(got), {c2, cl})
        got = self.search("zebra", until="2026-09-15")  # the whole day
        self.assertEqual(self.ids(got), {c1, c2})
        got = self.search("zebra", until="2026-09-15T09:00:00.000Z")
        self.assertEqual(self.ids(got), {c1})
        got = self.search("zebra", since="2026-09-02", until="2026-09-29")
        self.assertEqual(self.ids(got), {c2})

    def test_bad_arguments_return_error(self) -> None:
        self.one("a", "zebra")
        cases = [
            ({"kinds": {"bogus"}}, "bad_kind"),
            ({"provider": "gemini"}, "bad_provider"),
            ({"since": "yesterday"}, "bad_date"),
            ({"until": "2026-13-45"}, "bad_date"),
        ]
        for kw, code in cases:
            with self.subTest(kw):
                got = self.search("zebra", **kw)
                self.assertEqual(got, {"error": code, "notice": NOTICE})

    def ev(self, name: str, text: str, cls: str = "primary", **kw: Any) -> int:
        """Add one event in its own source of the given class.

        Args:
            name: The thread id of the new source.
            text: The event text.
            cls: The thread class of the source.
            **kw: Event overrides, as for ``add_event``.

        Returns:
            The event id.
        """
        src = self.add_source(name, cls=cls)
        return self.add_event(src, self.repo, text, **kw)

    def test_include_subagents_and_delegation_kind(self) -> None:
        prompt = self.ev("p", "marker prompt")
        deleg = self.ev("s1", "marker do it", "subagent", kind="delegation")
        sreply = self.ev("s2", "marker done", "subagent", kind="reply")
        scall = self.ev("s3", "marker call", "subagent", kind="tool_call")
        self.noise(self.repo)
        self.assertEqual(self.ids(self.search("marker")), {prompt})
        wide = self.search("marker", include_subagents=True)
        self.assertEqual(self.ids(wide), {prompt, deleg, sreply})
        calls = self.search(
            "marker", include_subagents=True, kinds={"tool_call"}
        )
        self.assertEqual(self.ids(calls), {scall})
        by_id = {h["id"]: h for h in wide["hits"]}
        self.assertEqual(by_id[deleg]["class"], "subagent")
        self.assertEqual(by_id[deleg]["kind"], "delegation")
        self.assertNotIn("class", by_id[prompt])
        # delegation is a subagent kind: naming it does not opt in
        got = self.search("marker", kinds={"delegation"})
        self.assertEqual(got["hits"], [])

    def test_include_subagents_adds_agent_message_reports(self) -> None:
        prompt = self.ev("p", "marker prompt")
        report = self.ev(
            "h1", "marker child report", kind="harness", tag="agent_message"
        )
        env = self.ev(
            "h2", "marker env", kind="harness", tag="environment_context"
        )
        bare = self.ev("h3", "marker untagged", kind="harness")
        self.noise(self.repo)
        self.assertEqual(self.ids(self.search("marker")), {prompt})
        wide = self.search("marker", include_subagents=True)
        self.assertEqual(self.ids(wide), {prompt, report})  # not env/bare
        by_id = {h["id"]: h for h in wide["hits"]}
        self.assertEqual(by_id[report]["tag"], "agent_message")
        self.assertNotIn("class", by_id[report])  # a primary thread's event
        harness = self.search("marker", kinds={"harness"})
        self.assertEqual(self.ids(harness), {env, bare})  # reports: opt in
        both = self.search("marker", kinds={"harness"}, include_subagents=True)
        self.assertEqual(self.ids(both), {env, bare, report})

    def test_since_and_until_are_inclusive_at_a_timestamp(self) -> None:
        at = "2026-09-15T10:00:00.000Z"
        hit = self.one("a", "zebra", ts=at)
        self.noise(self.repo)
        for kw in ({"since": at}, {"until": at}):
            with self.subTest(kw):
                self.assertEqual(self.ids(self.search("zebra", **kw)), {hit})
        early = "2026-09-15T09:59:59.999Z"
        self.assertEqual(self.search("zebra", until=early)["hits"], [])
        late = "2026-09-15T10:00:00.001Z"
        self.assertEqual(self.search("zebra", since=late)["hits"], [])

    def test_explicit_session_beats_the_current_session_exclusion(
        self,
    ) -> None:
        mine = self.one("cur", "canary echo")
        self.one("old", "canary echo")
        self.noise(self.repo)
        env = {"CLAUDE_CODE_SESSION_ID": "cur"}
        got = self.search("canary", env=env, session="cur")
        self.assertEqual(self.ids(got), {mine})

    def test_delegation_needs_the_opt_in_even_in_a_primary_thread(
        self,
    ) -> None:
        deleg = self.ev("p", "marker odd", kind="delegation")
        self.noise(self.repo)
        self.assertEqual(self.search("marker")["hits"], [])
        got = self.search("marker", include_subagents=True)
        self.assertEqual(self.ids(got), {deleg})

    def test_limit_and_page_are_clamped(self) -> None:
        for i in range(3):
            self.one(f"s{i}", f"zebra {'pad ' * i}")
        self.noise(self.repo)
        self.assertEqual(self.search("zebra", limit=1000)["limit"], 30)
        for limit in (0, -5):
            got = self.search("zebra", limit=limit)
            self.assertEqual((got["limit"], len(got["hits"])), (1, 1))
        self.assertEqual(self.search("zebra", page=-3)["page"], 1)

    def test_tool_error_only_with_explicit_kind(self) -> None:
        p = self.add_source("p")
        call = self.add_event(
            p, self.repo, "marker run", kind="tool_call", role="assistant"
        )
        err = self.add_event(
            p, self.repo, "marker traceback", kind="tool_error",
            role="assistant", parent=call,
        )  # fmt: skip
        self.noise(self.repo)
        self.assertEqual(self.ids(self.search("marker")), set())
        wide = self.search("marker", include_subagents=True)
        self.assertEqual(self.ids(wide), set())
        got = self.search("marker", kinds={"tool_error"})
        self.assertEqual(self.ids(got), {err})
        self.assertEqual(
            self.ids(self.search("marker", kinds=["tool_error"])), {err}
        )

    def test_exact_cwd_scope_filter(self) -> None:
        at = self.one("a", "zebra one", cwd="/repo")
        sub = self.one("b", "zebra two", cwd="/repo/sub")
        self.one("c", "zebra three", cwd="/repo2")
        self.one("d", "zebra four")
        self.noise(self.repo)
        self.assertEqual(len(self.search("zebra")["hits"]), 4)  # repo scope
        got = self.search("zebra", cwd="/repo", scope="/repo")
        self.assertEqual(self.ids(got), {at})
        got = self.search("zebra", cwd="/elsewhere", scope="/repo/sub")
        self.assertEqual(self.ids(got), {sub})
