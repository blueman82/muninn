"""Knowledge contract: add / retract / list / show / check / block_entries.

Synthetic rows only, in temp dirs; the cited text is invented. Rows go in
through the query test builders and are read back with plain SQL.
"""

from pctx import knowledge
from tests import test_query as tq

PROMPT = "We decided to use the zebra cache for every lookup."
REPLY = "Understood, I will wire the zebra cache into the lookup path."
CALL = "Bash: pytest -q tests/test_lookup.py"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123"


class KnowCase(tq.QueryCase):
    """A repo scope with one primary thread: a prompt, a reply, a call."""

    def setUp(self):
        super().setUp()
        self.repo = self.add_scope("/repo")
        self.src = self.add_source("thr-main", session="sess-main")
        self.prompt = self.add_event(
            self.src, self.repo, PROMPT, ts="2026-09-01T10:00:00.000Z"
        )
        self.reply = self.add_event(
            self.src, self.repo, REPLY, kind="reply", role="assistant",
            ts="2026-09-01T10:00:05.000Z",
        )  # fmt: skip
        self.call = self.add_event(
            self.src, self.repo, CALL, kind="tool_call", role="assistant",
            tag="Bash", ts="2026-09-01T10:00:09.000Z",
        )  # fmt: skip

    def ref(self, event_id):
        row = self.rw.execute(
            "SELECT s.provider, s.thread_id, e.line, e.part FROM event e"
            " JOIN source s ON s.id = e.source_id WHERE e.id = ?",
            (event_id,),
        ).fetchone()
        return f"{row[0]}:{row[1]}:{row[2]}.{row[3]}"

    def add(self, **kw):
        args = {
            "kind": "decision",
            "text": "Use the zebra cache for lookups",
            "cites": [(self.ref(self.prompt), "use the zebra cache")],
            "quote_only": None,
            "supersedes": None,
            "global_scope": False,
            "cwd": "/repo",
            "actor": "claude:abc123",
            "roots": {},
            "env": {},
        }
        return knowledge.add(self.rw, **(args | kw))

    def counts(self):
        tables = ("knowledge", "citation", "knowledge_log", "scope")
        return {
            t: self.rw.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in (*tables, "scope_path")
        }

    def refused(self, code, **kw):
        """add() must refuse with `code` and write nothing at all."""
        before = self.counts()
        with self.assertRaises(knowledge.Refused) as caught:
            self.add(**kw)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.counts(), before)
        return caught.exception

    def other_event(self, text, cls="primary", **kw):
        name = f"thr-{cls}-{self.rw.total_changes}"
        src = self.add_source(name, cls=cls)
        return self.add_event(src, self.repo, text, **kw)


class AddCitationTests(KnowCase):
    def test_add_requires_citation(self):
        self.assertEqual(knowledge.Refused("uncited").code, "uncited")
        self.refused("uncited", cites=[])
        self.refused("uncited", cites=[], cwd="/brand/new")  # no scope row

    def test_quote_must_be_verbatim_substring(self):
        ref = self.ref(self.prompt)
        self.refused("quote_not_found", cites=[(ref, "use the yak cache")])
        bad = [(ref, "use the yak cache")]  # the scope row rolls back too
        self.refused("quote_not_found", cites=bad, cwd="/brand/new")
        self.refused("quote_not_found", cites=[(ref, "USE THE ZEBRA CACHE")])
        got = self.add(cites=[(ref, "decided to  use\n the   zebra cache")])
        (cite,) = got["entry"]["cites"]
        self.assertEqual(cite["quote"], "decided to use the zebra cache")
        start, end = cite["span"]
        self.assertEqual(" ".join(PROMPT[start:end].split()), cite["quote"])

    def test_quote_length_is_12_to_300_collapsed(self):
        ref = self.ref(self.prompt)
        self.refused("quote_length", cites=[(ref, "use the zebr"[:11])])
        self.refused("quote_length", cites=[(ref, "")])
        self.refused("quote_length", cites=[(ref, " \n ")])
        self.add(cites=[(ref, "use the zebr")])  # exactly 12
        long_ref = self.ref(self.other_event("a" * 400))
        self.add(cites=[(long_ref, "a" * 300)])
        self.refused("quote_length", cites=[(long_ref, "a" * 301)])

    def test_harness_subagent_events_not_citable(self):
        cases = {
            "harness": self.other_event(
                "<environment_context> the repo cwd", kind="harness"
            ),
            "delegation": self.other_event(
                "please investigate the cache", "subagent", kind="delegation"
            ),
            "subagent reply": self.other_event(
                "the cache investigation is done", "subagent", kind="reply"
            ),
            "subagent call": self.other_event(
                "Bash: cat cache.py", "subagent", kind="tool_call"
            ),
            "reviewer": self.other_event("review this change", "reviewer"),
            "other class": self.other_event("handed off notes", "other"),
        }
        for name, event in cases.items():
            with self.subTest(name):
                text = self.rw.execute(
                    "SELECT text FROM event WHERE id = ?", (event,)
                ).fetchone()[0]
                self.refused("not_citable", cites=[(self.ref(event), text)])

    def test_flagged_and_subagent_events_not_citable(self):
        pasted = self.other_event("a pasted block of memory", flags=1)
        error = self.other_event(
            "AssertionError: lookup failed", kind="tool_error"
        )
        for event, quote in (
            (pasted, "pasted block"),
            (error, "lookup failed"),
        ):
            self.refused("not_citable", cites=[(self.ref(event), quote)])
        # a citable neighbour in the same entry does not rescue it
        good = (self.ref(self.prompt), "use the zebra cache")
        self.refused(
            "not_citable", cites=[good, (self.ref(pasted), "pasted block")]
        )
        self.add(cites=[(self.ref(self.reply), "wire the zebra cache")])
        self.add(cites=[(self.ref(self.call), "pytest -q tests")])

    def test_ref_forms_and_error_codes(self):
        quote = "use the zebra cache"
        for ref in (
            self.ref(self.prompt),
            "codex:thr-main:1",  # part defaults to 1
            "codex:thr-ma:1.1",  # an unambiguous thread prefix
            str(self.prompt),  # an event id
        ):
            with self.subTest(ref):
                got = self.add(cites=[(ref, quote)])
                cite = got["entry"]["cites"][0]
                self.assertEqual(cite["ref"], "codex:thr-main:1.1")
        self.add_event(self.add_source("thr-mailbox"), self.repo, PROMPT)
        codes = {
            "codex:thr-ma:1.1": "ambiguous_ref",
            "codex:thr-main:99.1": "not_found",
            "999999": "not_found",
            "garbage": "bad_ref",
            "": "bad_ref",
        }
        for ref, code in codes.items():
            with self.subTest(ref):
                self.refused(code, cites=[(ref, quote)])


class AddEntryTests(KnowCase):
    def test_add_stores_entry_citation_identity_and_log(self):
        got = self.add(kind="fact", text="  The lookup path uses the cache  ")
        entry = got["entry"]
        self.assertEqual(got["notice"], tq.NOTICE)
        row = self.rw.execute("SELECT * FROM knowledge").fetchone()
        self.assertEqual(entry["id"], f"K{row['id']}")
        self.assertEqual(
            tuple(row)[1:9],
            (self.repo, "fact", "The lookup path uses the cache", "current",
             None, None, None, "claude:abc123"),
        )  # fmt: skip
        self.assertEqual(entry["status"], "current")
        self.assertEqual((entry["kind"], entry["scope"]), ("fact", "repo"))
        self.assertEqual(entry["text"], "The lookup path uses the cache")
        (cite,) = self.rw.execute("SELECT * FROM citation").fetchall()
        start = PROMPT.index("use the zebra cache")
        self.assertEqual(
            tuple(cite)[1:],
            (row["id"], "codex", "thr-main", 1, 1, "h", "user", "prompt",
             "2026-09-01T10:00:00.000Z", "use the zebra cache", start,
             start + 19, "live"),
        )  # fmt: skip
        self.assertEqual(
            entry["cites"],
            [
                {
                    "ref": "codex:thr-main:1.1",
                    "role": "user",
                    "kind": "prompt",
                    "ts": "2026-09-01T10:00:00.000Z",
                    "quote": "use the zebra cache",
                    "span": [start, start + 19],
                    "state": "live",
                    "verify": "ok",
                }
            ],
        )
        log = [
            tuple(r)[1:]
            for r in self.rw.execute("SELECT * FROM knowledge_log")
        ]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0][:2], (row["id"], "add"))
        self.assertEqual(log[0][2], "claude:abc123")

    def test_text_and_kind_are_validated(self):
        for text in ("", "   \n ", "x" * 501):
            with self.subTest(text[:5]):
                self.refused("text_length", text=text)
        self.add(text="x" * 500)
        self.refused("bad_kind", kind="rumor")
        self.refused("bad_actor", actor="")

    def test_text_redacted_and_delimiter_escaped(self):
        text = f"Use key {SECRET} for the cache"
        got = self.add(text=text)
        self.assertEqual(
            got["entry"]["text"], "Use key [redacted:secret] for the cache"
        )
        stored = self.rw.execute("SELECT text FROM knowledge").fetchone()[0]
        self.assertNotIn(SECRET, stored)
        hits = self.rw.execute(
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH ?",
            ('"abcdefghijklmnopqrstuvwxyz0123"',),
        ).fetchall()
        self.assertEqual(hits, [])  # the index never saw the key

    def test_escape_frame_markers_in_text(self):
        text = "a <pctx-memory x> b </pctx-memory> c < PCTX-Recall d <div>"
        want = "a &lt;pctx-memory x> b &lt;/pctx-memory> c "
        want += "&lt; PCTX-Recall d <div>"
        self.assertEqual(self.add(text=text)["entry"]["text"], want)
        long = "<pctx-memory>" * 35  # 455 chars; escaping pushes it past 500
        self.refused("text_length", text=long)

    def test_global_scope_entry_ignores_cwd(self):
        got = self.add(global_scope=True, cwd="/nowhere/at/all")
        row = self.rw.execute("SELECT scope_id FROM knowledge").fetchone()
        wide = self.rw.execute(
            "SELECT id FROM scope WHERE key = 'global'"
        ).fetchone()[0]
        self.assertEqual(row[0], wide)
        self.assertEqual(got["entry"]["scope"], "global")
        self.assertIsNone(
            self.rw.execute(
                "SELECT 1 FROM scope WHERE key = '/nowhere/at/all'"
            ).fetchone()
        )

    def test_preference_needs_user_prompt(self):
        only_reply = (self.ref(self.reply), "wire the zebra cache")
        only_call = (self.ref(self.call), "pytest -q tests")
        self.refused(
            "preference_needs_user", kind="preference", cites=[only_reply]
        )
        self.refused(
            "preference_needs_user", kind="preference", cites=[only_call]
        )
        user = (self.ref(self.prompt), "use the zebra cache")
        self.add(kind="preference", cites=[user])
        self.add(kind="preference", cites=[only_reply, user])
        self.add(kind="decision", cites=[only_reply])  # only preferences ask


def kid(got):
    return int(got["entry"]["id"][1:])


class LedgerTests(KnowCase):
    def log(self, kind_id):
        return [
            tuple(r)
            for r in self.rw.execute(
                "SELECT action, actor FROM knowledge_log"
                " WHERE knowledge_id = ? ORDER BY id",
                (kind_id,),
            )
        ]

    def row(self, kind_id):
        return self.rw.execute(
            "SELECT * FROM knowledge WHERE id = ?", (kind_id,)
        ).fetchone()

    def test_supersede_chain_and_log(self):
        one = kid(self.add(text="Use the zebra cache", actor="user"))
        two = kid(self.add(text="Use the yak cache", supersedes=one))
        three = kid(self.add(text="Use both caches", supersedes=f"K{two}"))
        self.assertEqual(
            [self.row(k)["status"] for k in (one, two, three)],
            ["superseded", "superseded", "current"],
        )
        self.assertEqual(
            [
                (self.row(k)["supersedes"], self.row(k)["superseded_by"])
                for k in (one, two, three)
            ],
            [(None, two), (one, three), (two, None)],
        )
        self.assertEqual(
            self.log(one), [("add", "user"), ("superseded", "claude:abc123")]
        )
        self.assertEqual(
            self.log(two),
            [
                ("add", "claude:abc123"),
                ("supersede", "claude:abc123"),
                ("superseded", "claude:abc123"),
            ],
        )
        self.assertEqual(
            self.log(three),
            [("add", "claude:abc123"), ("supersede", "claude:abc123")],
        )
        self.assertEqual(self.row(one)["text"], "Use the zebra cache")  # kept
        shown = knowledge.show(self.ro(), two)
        self.assertEqual(
            [e["id"] for e in shown["chain"]["supersedes"]], [f"K{one}"]
        )
        self.assertEqual(
            [e["id"] for e in shown["chain"]["superseded_by"]], [f"K{three}"]
        )
        self.assertEqual(shown["entry"]["supersedes"], f"K{one}")
        self.assertEqual(shown["entry"]["superseded_by"], f"K{three}")
        ends = knowledge.show(self.ro(), three)["chain"]
        self.assertEqual(
            [e["id"] for e in ends["supersedes"]], [f"K{two}", f"K{one}"]
        )
        self.assertEqual(ends["superseded_by"], [])
        ends = knowledge.show(self.ro(), one)["chain"]
        self.assertEqual(
            [e["id"] for e in ends["superseded_by"]], [f"K{two}", f"K{three}"]
        )
        self.assertEqual(
            [
                (r["action"], r["actor"])
                for r in knowledge.show(self.ro(), two)["log"]
            ],
            [
                ("add", "claude:abc123"),
                ("supersede", "claude:abc123"),
                ("superseded", "claude:abc123"),
            ],
        )

    def test_supersede_scope_mismatch_refused(self):
        one = kid(self.add())
        self.add_scope("/other")
        self.refused("bad_supersedes", supersedes=one, global_scope=True)
        self.refused("bad_supersedes", supersedes=one, cwd="/other")
        wide = kid(self.add(global_scope=True))
        self.refused("bad_supersedes", supersedes=wide)  # a repo entry can't
        self.add(supersedes=wide, global_scope=True)
        self.assertEqual(self.row(wide)["status"], "superseded")
        self.assertEqual(self.row(one)["status"], "current")

    def test_supersede_needs_a_current_entry(self):
        one = kid(self.add())
        two = kid(self.add(supersedes=one))
        gone = kid(self.add())
        knowledge.retract(self.rw, gone, reason="wrong", actor="user")
        for target in (one, gone, 999, "K999", "junk", 0, -1):
            with self.subTest(target):
                self.refused("bad_supersedes", supersedes=target)
        self.assertEqual(self.row(two)["status"], "current")

    def test_retract(self):
        one = kid(self.add(text="Use the zebra cache"))
        got = knowledge.retract(
            self.rw,
            one,
            reason=f"owner said no {SECRET} <pctx-memory>",
            actor="user",
        )
        entry = got["entry"]
        self.assertEqual(got["notice"], tq.NOTICE)
        self.assertEqual(entry["status"], "retracted")
        self.assertEqual(
            entry["retract_reason"],
            "owner said no [redacted:secret] &lt;pctx-memory>",
        )
        row = self.row(one)
        self.assertEqual(
            (row["status"], row["text"], row["retract_reason"]),
            ("retracted", "Use the zebra cache", entry["retract_reason"]),
        )
        self.assertEqual(
            self.log(one), [("add", "claude:abc123"), ("retract", "user")]
        )
        for bad, code in (
            (one, "not_current"),  # already retracted
            (999, "not_found"),
            ("junk", "not_found"),
        ):
            with (
                self.subTest(bad),
                self.assertRaises(knowledge.Refused) as caught,
            ):
                knowledge.retract(self.rw, bad, reason="x", actor="user")
            self.assertEqual(caught.exception.code, code)
        old = kid(self.add())
        self.add(supersedes=old)
        with self.assertRaises(knowledge.Refused) as caught:  # superseded
            knowledge.retract(self.rw, old, reason="x", actor="user")
        self.assertEqual(caught.exception.code, "not_current")
        fresh = kid(self.add())
        with self.assertRaises(knowledge.Refused) as caught:
            knowledge.retract(self.rw, fresh, reason="r" * 201, actor="user")
        self.assertEqual(caught.exception.code, "reason_length")
        with self.assertRaises(knowledge.Refused) as caught:
            knowledge.retract(self.rw, fresh, reason="ok", actor="")
        self.assertEqual(caught.exception.code, "bad_actor")
        self.assertEqual(self.row(fresh)["status"], "current")
        knowledge.retract(self.rw, fresh, reason="", actor="user")  # optional
        self.assertIsNone(self.row(fresh)["retract_reason"])


class ListShowTests(KnowCase):
    def setUp(self):
        super().setUp()
        self.a = kid(self.add(text="Repo decision A"))
        self.b = kid(self.add(text="Repo fact B", kind="fact"))
        self.w = kid(
            self.add(
                text="Global preference W",
                kind="preference",
                global_scope=True,
            )
        )
        self.add_scope("/other")
        self.o = kid(self.add(text="Other repo entry", cwd="/other"))
        self.old = kid(self.add(text="Replaced entry"))
        self.new = kid(self.add(text="Replacement", supersedes=self.old))
        knowledge.retract(self.rw, self.b, reason="not true", actor="user")

    def ids(self, **kw):
        got = knowledge.list_entries(self.ro(), **({"cwd": "/repo"} | kw))
        return [int(e["id"][1:]) for e in got["entries"]]

    def test_list_entries_scope_status_kind_order(self):
        a, b, w, o, old, new = (
            self.a,
            self.b,
            self.w,
            self.o,
            self.old,
            self.new,
        )
        self.assertEqual(
            self.ids(), [new, w, a]
        )  # repo + global, newest first
        self.assertEqual(self.ids(all_projects=True), [new, o, w, a])
        self.assertEqual(self.ids(cwd="/other"), [o, w])
        self.assertEqual(self.ids(cwd="/nowhere"), [w])  # global only
        self.assertEqual(self.ids(status="all"), [new, old, w, b, a])
        self.assertEqual(self.ids(status="superseded"), [old])
        self.assertEqual(self.ids(status="retracted"), [b])
        self.assertEqual(self.ids(kind="preference"), [w])
        self.assertEqual(self.ids(status="all", kind="fact"), [b])
        bad = knowledge.list_entries(self.ro(), cwd="/repo", status="old")
        self.assertEqual(bad, {"error": "bad_status", "notice": tq.NOTICE})
        bad = knowledge.list_entries(self.ro(), cwd="/repo", kind="rumor")
        self.assertEqual(bad, {"error": "bad_kind", "notice": tq.NOTICE})

    def test_list_entries_carry_actor_date_and_citation_states(self):
        got = knowledge.list_entries(self.ro(), cwd="/repo")
        self.assertEqual((got["notice"], got["count"]), (tq.NOTICE, 3))
        entry = got["entries"][-1]  # the oldest: A
        self.assertEqual(entry["id"], f"K{self.a}")
        self.assertEqual(entry["actor"], "claude:abc123")
        self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual([c["verify"] for c in entry["cites"]], ["ok"])

    def test_show_ids_and_unknown(self):
        ro = self.ro()
        for ref in (self.new, f"K{self.new}", f"k{self.new}", str(self.new)):
            with self.subTest(ref):
                self.assertEqual(
                    knowledge.show(ro, ref)["entry"]["id"], f"K{self.new}"
                )
        for ref in (999, "junk", "K", None):
            with self.subTest(ref):
                self.assertEqual(
                    knowledge.show(ro, ref),
                    {"error": "not_found", "notice": tq.NOTICE},
                )
        retracted = knowledge.show(ro, self.b)["entry"]
        self.assertEqual(retracted["retract_reason"], "not true")
        self.assertEqual(
            [
                (r["action"], r["actor"])
                for r in knowledge.show(ro, self.b)["log"]
            ],
            [("add", "claude:abc123"), ("retract", "user")],
        )
        log = knowledge.show(ro, self.a)["log"][0]
        self.assertRegex(log["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
