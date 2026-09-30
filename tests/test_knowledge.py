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
