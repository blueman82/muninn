"""Knowledge contract: add / retract / list / show / check / block_entries.

Synthetic rows only, in temp dirs; the cited text is invented. Rows go in
through the query test builders and are read back with plain SQL.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

from pctx import erase, ingest, knowledge, store
from tests import test_classify as tc
from tests import test_ingest as ti
from tests import test_query as tq
from tests import test_store as tst

ROOT = Path(__file__).resolve().parent.parent

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


class VerifyTests(KnowCase):
    def first(self):
        return self.rw.execute("SELECT * FROM citation ORDER BY id").fetchone()

    def state(self, row=None):
        return knowledge.verify_citation(self.ro(), row or self.first())

    def reparse(self, event_id, digest="h2", text=PROMPT):
        """What a replace-mode pass does: the line's events are deleted and
        re-created, here with another hash or text."""
        old = self.rw.execute(
            "SELECT source_id, scope_id, line, part, ts FROM event"
            " WHERE id = ?",
            (event_id,),
        ).fetchone()
        self.rw.execute("DELETE FROM event WHERE id = ?", (event_id,))
        return self.add_event(
            old["source_id"], old["scope_id"], text, line=old["line"],
            part=old["part"], ts=old["ts"], digest=digest,
        )  # fmt: skip

    def test_verify_citation_states(self):
        self.add()
        self.assertEqual(self.state(), "ok")
        again = self.reparse(self.prompt, digest="h")  # same line, same hash
        self.assertEqual(self.state(), "ok")
        again = self.reparse(again, digest="h2")  # the line changed
        self.assertEqual(self.state(), "changed")
        again = self.reparse(again, digest="h", text="We chose the yak cache.")
        self.assertEqual(self.state(), "changed")  # hash back, quote gone
        self.reparse(again, digest="h", text=f"{PROMPT} More words.")
        self.assertEqual(self.state(), "ok")
        self.rw.execute("DELETE FROM event WHERE line = 1")  # line vanished
        self.assertEqual(self.state(), "changed")

    def test_missing_source_and_erased_citation(self):
        self.add()
        self.rw.execute("UPDATE source SET status = 'missing'")
        self.assertEqual(
            self.state(), "missing"
        )  # the kept event still says so
        self.reparse(self.prompt, digest="h2")
        self.assertEqual(self.state(), "changed")  # a change outranks missing
        self.rw.execute("DELETE FROM event")
        self.rw.execute("DELETE FROM source")  # nothing left to look at
        self.assertEqual(self.state(), "missing")
        self.rw.execute(
            "UPDATE citation SET quote = NULL, span_start = NULL,"
            " span_end = NULL, state = 'erased'"
        )
        self.assertEqual(self.state(), "erased")

    def test_check_counts_and_names_the_broken(self):
        def cite(event, quote):
            return [(self.ref(event), quote)]

        ok = kid(self.add(text="Fine entry"))
        changed = kid(
            self.add(
                text="Changed entry",
                cites=cite(self.reply, "wire the zebra cache"),
            )
        )
        far = self.other_event("a remote decision was made here")
        gone = kid(
            self.add(text="Missing entry", cites=cite(far, "remote decision"))
        )
        erased = kid(
            self.add(
                text="Erased cite", cites=cite(self.call, "pytest -q tests")
            )
        )
        self.reparse(self.reply, digest="h2")
        self.rw.execute(
            "UPDATE source SET status = 'missing'"
            " WHERE id = (SELECT source_id FROM event WHERE id = ?)",
            (far,),
        )
        self.rw.execute(
            "UPDATE citation SET quote = NULL, span_start = NULL,"
            " span_end = NULL, state = 'erased' WHERE knowledge_id = ?",
            (erased,),
        )
        got = knowledge.check(self.ro())
        self.assertEqual(got["notice"], tq.NOTICE)
        self.assertEqual(got["citations"], 4)
        counts = (got["ok"], got["changed"], got["missing"], got["erased"])
        self.assertEqual(counts, (1, 1, 1, 1))
        self.assertEqual(
            [(p["id"], p["state"]) for p in got["problems"]],
            [(f"K{changed}", "changed"), (f"K{gone}", "missing")],
        )
        self.assertEqual(got["problems"][0]["ref"], "codex:thr-main:2.1")
        self.assertEqual(got["problems"][0]["status"], "current")
        self.assertNotIn(f"K{ok}", [p["id"] for p in got["problems"]])
        self.assertNotIn("zebra", json.dumps(got))  # refs, never text

    def test_survives_new_process(self):
        self.add(text="The cache is the zebra cache")
        code = (
            "import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from pathlib import Path\n"
            "from pctx import knowledge, store\n"
            "conn = store.connect_ro(Path(sys.argv[2]))\n"
            "print(json.dumps(knowledge.list_entries(conn, cwd='/repo')))\n"
        )
        done = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(ROOT), str(self.db)],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        (entry,) = json.loads(done.stdout)["entries"]
        self.assertEqual(entry["text"], "The cache is the zebra cache")
        self.assertEqual(entry["status"], "current")
        self.assertEqual([c["verify"] for c in entry["cites"]], ["ok"])

    def test_erase_marks_citations_and_entries(self):
        both = kid(
            self.add(
                cites=[
                    (self.ref(self.prompt), "use the zebra cache"),
                    (self.ref(self.reply), "wire the zebra cache"),
                ]
            )
        )
        lone = kid(
            self.add(
                text="Only the call",
                cites=[(self.ref(self.call), "pytest -q tests")],
            )
        )
        gone = self.ref(self.call)
        erase.erase(self.rw, home=self.home, event_ref=gone, env={})
        entry = knowledge.show(self.ro(), lone)["entry"]
        self.assertEqual((entry["status"], entry["text"]), ("erased", None))
        self.assertEqual([c["verify"] for c in entry["cites"]], ["erased"])
        self.assertIsNone(entry["cites"][0]["quote"])
        erased = knowledge.list_entries(
            self.ro(), cwd="/repo", status="erased"
        )
        self.assertEqual(erased["count"], 1)
        erase.erase(
            self.rw, home=self.home, event_ref=self.ref(self.reply), env={}
        )
        partly = knowledge.show(self.ro(), both)["entry"]
        self.assertEqual(partly["status"], "current")  # one live cite is left
        verdicts = [c["verify"] for c in partly["cites"]]
        self.assertEqual(verdicts, ["ok", "erased"])
        got = knowledge.check(self.ro())
        self.assertEqual((got["ok"], got["erased"], got["changed"]), (1, 2, 0))


class ReparseTests(ti.IngestCase):
    """check() against what a real ingest pass does to a cited line."""

    def setUp(self):
        super().setUp()
        self.tid = ti.TID
        self.path = self.write(
            ti.rollout(self.tid), self.records("keep the zebra cache", "noted")
        )
        self.run_ingest()

    def records(self, question, answer):
        return [
            tc.codex_meta("user", self.tid),
            tc.user_msg(1, question),
            tc.reply(2, answer),
        ]

    def cite(self):
        return knowledge.add(
            self.conn, kind="decision", text="Keep the zebra cache",
            cites=[(f"codex:{self.tid}:2.1", "keep the zebra cache")],
            quote_only=None, supersedes=None, global_scope=False,
            cwd=tc.CWD, actor="user", roots=self.roots, env={},
        )  # fmt: skip

    def test_check_detects_changed_after_reparse(self):
        self.cite()
        self.assertEqual(knowledge.check(self.conn)["ok"], 1)
        # the last line moves, so ingest replaces the source; the cited
        # line keeps its position but not its bytes
        changed = self.records("drop the zebra cache", "noted!")
        self.write(ti.rollout(self.tid), changed)
        self.run_ingest()
        got = knowledge.check(self.conn)
        self.assertEqual((got["ok"], got["changed"]), (0, 1))
        self.assertEqual(got["problems"][0]["ref"], f"codex:{self.tid}:2.1")

    def test_provider_deleting_the_file_makes_citations_missing(self):
        self.cite()
        self.path.unlink()
        self.run_ingest()
        got = knowledge.check(self.conn)
        self.assertEqual((got["ok"], got["missing"]), (0, 1))


class ApprovalTests(KnowCase):
    """O5a: a quote under 12 characters is an approval of a proposal."""

    def setUp(self):
        super().setUp()
        src = self.add_source("thr-talk", session="sess-talk")
        self.n = 0

        def say(text, **kw):
            self.n += 1
            kw.setdefault("ts", f"2026-09-02T09:00:{self.n:02d}.000Z")
            return self.add_event(src, self.repo, text, **kw)

        def bot(text, **kw):
            return say(text, kind="reply", role="assistant", **kw)

        say("Please design the lookup path.")
        self.r0 = bot("Sure, here is a first outline of the lookup path.")
        self.r1 = bot(
            "I propose to cache lookups in the zebra cache. Shall I?"
        )
        self.c1 = say("Bash: ls src", kind="tool_call", role="assistant")
        self.yes = say("yes, do it")
        self.r2 = bot("Done, the zebra cache is wired in.")
        self.short_reply = bot("Done.")
        self.r3 = bot("Shall I also cache the yak lookups too?")
        self.yes2 = say("yes, do it")
        solo = self.add_source("thr-solo", session="sess-solo")
        self.first = self.add_event(solo, self.repo, "go ahead")
        other = self.add_source("thr-other", session="sess-other")
        self.add_event(
            other,
            self.repo,
            "an unrelated question",
            ts="2026-09-02T08:00:00.000Z",
        )
        self.r_other = self.add_event(
            other,
            self.repo,
            "I propose to cache lookups elsewhere",
            kind="reply",
            role="assistant",
        )
        self.quote = {
            self.r0: "first outline of the lookup",
            self.r1: "propose to cache lookups",
            self.r2: "zebra cache is wired in",
            self.r3: "also cache the yak lookups",
            self.r_other: "propose to cache lookups",
        }

    def cite(self, event, quote=None):
        return (self.ref(event), quote or self.quote[event])

    def test_short_approval_needs_preceding_reply_citation(self):
        yes = self.cite(self.yes, "yes, do it")
        self.refused("approval_needs_reply", cites=[yes])
        # an earlier reply, a later reply, another thread's reply: not it
        for wrong in (self.r0, self.r2, self.r_other):
            with self.subTest(wrong):
                self.refused(
                    "approval_needs_reply", cites=[yes, self.cite(wrong)]
                )
        got = self.add(cites=[yes, self.cite(self.r1)])
        self.assertEqual(
            [(c["ref"], c["quote"]) for c in got["entry"]["cites"]],
            [
                (self.ref(self.yes), "yes, do it"),
                (self.ref(self.r1), "propose to cache lookups"),
            ],
        )
        self.add(kind="preference", cites=[yes, self.cite(self.r1)])
        spaced = (self.ref(self.yes), "yes,do it")
        self.refused("quote_not_found", cites=[spaced, self.cite(self.r1)])
        spaced = (self.ref(self.yes), "  yes,   do  it ")
        self.add(cites=[spaced, self.cite(self.r1)])
        # the second "yes, do it" answers the later proposal
        yes2 = self.cite(self.yes2, "yes, do it")
        self.refused("approval_needs_reply", cites=[yes2, self.cite(self.r1)])
        self.add(cites=[yes2, self.cite(self.r3)])
        # two approvals in one entry each need their own proposal
        self.refused(
            "approval_needs_reply", cites=[yes, yes2, self.cite(self.r1)]
        )
        self.add(cites=[yes, yes2, self.cite(self.r1), self.cite(self.r3)])

    def test_only_a_whole_user_prompt_can_be_short(self):
        r1 = self.cite(self.r1)
        self.refused("quote_length", cites=[(self.ref(self.yes), "yes"), r1])
        self.refused("quote_length", cites=[(self.ref(self.yes), "do it"), r1])
        self.refused(
            "quote_length", cites=[(self.ref(self.short_reply), "Done."), r1]
        )
        self.refused(
            "quote_length", cites=[(self.ref(self.c1), "Bash: ls"), r1]
        )
        self.refused(
            "quote_not_found", cites=[(self.ref(self.yes), "yes do it"), r1]
        )
        self.refused(
            "approval_needs_reply", cites=[(self.ref(self.first), "go ahead")]
        )  # nothing came before it
        self.refused(
            "approval_needs_reply",
            cites=[(self.ref(self.first), "go ahead"), r1],
        )

    def test_quote_only_approval_also_needs_the_reply(self):
        env = {"CODEX_SESSION_ID": "sess-talk"}
        self.refused(
            "approval_needs_reply", quote_only="yes, do it", cites=[], env=env
        )
        # the latest "yes, do it" is the second; it answers r3, not r1
        self.refused(
            "approval_needs_reply",
            quote_only="yes, do it",
            cites=[self.cite(self.r1)],
            env=env,
        )
        got = self.add(
            quote_only="yes, do it", cites=[self.cite(self.r3)], env=env
        )
        refs = [c["ref"] for c in got["entry"]["cites"]]
        self.assertEqual(refs, [self.ref(self.r3), self.ref(self.yes2)])


class QuoteOnlyTests(ti.IngestCase):
    """quote_only: the caller's own prompts, after a targeted ingest."""

    QUOTE = "the zebra cache stays in place"

    def setUp(self):
        super().setUp()
        self.tid = ti.TID
        self.other_tid = "0199aaaa-bbbb-4ccc-8ddd-111111111111"
        self.third_tid = "0199aaaa-bbbb-4ccc-8ddd-222222222222"
        self.path = self.write(
            ti.rollout(self.tid),
            [
                tc.codex_meta("user", self.tid),
                tc.user_msg(1, f"we decided {self.QUOTE}, old wording"),
                tc.reply(2, f"noted: {self.QUOTE}"),
                tc.user_msg(
                    3, f"<user_instructions>{self.QUOTE}</user_instructions>"
                ),
            ],
        )
        self.write(
            ti.rollout(self.other_tid),
            [
                tc.codex_meta("user", self.other_tid),
                tc.user_msg(1, f"elsewhere: {self.QUOTE}"),
            ],
        )
        self.run_ingest()
        # typed a moment ago: on disk, not yet ingested
        self.append(
            self.path, [tc.user_msg(4, f"final call: {self.QUOTE}, ship it")]
        )
        self.write(
            ti.rollout(self.third_tid),
            [
                tc.codex_meta("user", self.third_tid),
                tc.user_msg(1, "never ingested"),
            ],
        )

    def add(self, **kw):
        args = {
            "kind": "decision", "text": "The zebra cache stays",
            "cites": [], "quote_only": self.QUOTE, "supersedes": None,
            "global_scope": False, "cwd": tc.CWD, "actor": "codex:0199aaaa",
            "roots": self.roots, "env": {"CODEX_THREAD_ID": self.tid},
        }  # fmt: skip
        return knowledge.add(self.conn, **(args | kw))

    def test_quote_only_searches_callers_session_prompts(self):
        self.assertEqual(self.events()[-1][:3], (4, 1, "harness"))  # not yet
        got = self.add()
        (cite,) = got["entry"]["cites"]
        self.assertEqual(cite["role"], "user")
        self.assertEqual(cite["kind"], "prompt")
        self.assertEqual(cite["quote"], self.QUOTE)
        rows = {r[0]: r[3] for r in self.events()}
        newest = max(
            line for line, text in rows.items() if "final call" in text
        )
        self.assertEqual(cite["ref"], f"codex:{self.tid}:{newest}.1")
        start, end = cite["span"]
        self.assertEqual(rows[newest][start:end], self.QUOTE)
        self.assertEqual(got["entry"]["cites"][0]["verify"], "ok")
        # only the caller's threads were read: the third file stays unseen
        self.assertIsNone(self.source(self.third_tid))

    def test_quote_only_is_the_latest_prompt_never_reply_or_harness(self):
        self.append(self.path, [tc.reply(5, f"sure, {self.QUOTE}")])
        cite = self.add()["entry"]["cites"][0]
        self.assertEqual(cite["kind"], "prompt")
        self.assertTrue(cite["ref"].endswith(":5.1"))  # the appended prompt
        only_old = self.add(
            quote_only="we decided the zebra cache stays in place, old"
        )
        self.assertTrue(only_old["entry"]["cites"][0]["ref"].endswith(":2.1"))

    def test_quote_only_errors(self):
        self.refused_add("no_caller_session", env={})
        self.refused_add("no_caller_session", env={"CODEX_THREAD_ID": ""})
        self.refused_add("quote_not_found", quote_only="a quote nobody typed")
        self.refused_add("quote_length", quote_only="")
        self.refused_add("quote_length", quote_only="x" * 301)
        # another session's prompt is never used
        other = {"CODEX_THREAD_ID": self.other_tid}
        got = self.add(env=other, quote_only="elsewhere: the zebra cache")
        self.assertIn(self.other_tid, got["entry"]["cites"][0]["ref"])
        self.refused_add(
            "quote_not_found",
            quote_only=f"final call: {self.QUOTE}",
            env=other,
        )

    def refused_add(self, code, **kw):
        with self.assertRaises(knowledge.Refused) as caught:
            self.add(**kw)
        self.assertEqual(caught.exception.code, code)

    def test_targeted_ingest_same_connection_no_relock(self):
        with store.writer_lock(self.home, wait_s=0):  # as the CLI holds it
            with (
                mock.patch.object(
                    ingest, "ingest", wraps=ingest.ingest
                ) as spy,
                mock.patch.object(
                    ingest, "run_pass", side_effect=AssertionError("relocked")
                ),
            ):
                self.add()
        spy.assert_called_once()
        self.assertIs(spy.call_args.args[0], self.conn)
        self.assertEqual(spy.call_args.args[1], self.roots)
        self.assertEqual(spy.call_args.kwargs["only_threads"], {self.tid})
        with store.writer_lock(self.home, wait_s=0):  # released afterwards
            pass

    def test_ingest_only_with_a_caller_and_roots(self):
        with mock.patch.object(ingest, "ingest") as spy:
            self.add(
                cites=[
                    (f"codex:{self.tid}:2.1", "we decided the zebra cache")
                ],
                quote_only=None,
                env={},
            )
            self.add(
                cites=[
                    (f"codex:{self.tid}:2.1", "we decided the zebra cache")
                ],
                quote_only=None,
                roots={},
            )
        spy.assert_not_called()

    def test_claude_caller(self):
        main = f"-work-repo/{tc.SESSION}.jsonl"
        path = self.write(
            main,
            [tc.claude_rec("user", "earlier talk")],
            root="claude-projects",
        )
        self.run_ingest()
        self.append(
            path, [tc.claude_rec("user", f"remember {self.QUOTE} please")]
        )
        env = {"CLAUDE_CODE_SESSION_ID": tc.SESSION}
        cite = self.add(env=env)["entry"]["cites"][0]
        self.assertEqual(cite["ref"], f"claude:{tc.SESSION}:2.1")
        self.assertEqual(cite["quote"], self.QUOTE)


class BlockTests(KnowCase):
    """block_entries: what the SessionStart block may push (O5b)."""

    def cites(self, *pairs):
        return [(self.ref(event), quote) for event, quote in pairs]

    def test_block_entries_user_cited_only_limit8_with_quote(self):
        user = self.cites((self.prompt, "use the zebra cache"))
        reply = self.cites((self.reply, "wire the zebra cache"))
        call = self.cites((self.call, "pytest -q tests"))
        pushed = [
            kid(self.add(text=f"Decision {i}", cites=user)) for i in range(10)
        ]
        self.add(text="Reply only", cites=reply)
        self.add(text="Call only", cites=call)
        mixed = kid(
            self.add(
                text="Reply then prompt",
                cites=reply
                + self.cites((self.prompt, "decided to use the zebra")),
            )
        )
        wide = self.add_scope("/other")  # an entry of another repo
        self.add(text="Elsewhere", cites=user, cwd="/other")
        got = knowledge.block_entries(self.ro(), [self.repo])
        ids = [e["id"] for e in got]
        self.assertEqual(len(got), 8)  # the default limit
        self.assertEqual(
            ids, [f"K{k}" for k in [mixed, *reversed(pushed)][:8]]
        )
        first = got[0]
        self.assertEqual(
            first,
            {
                "id": f"K{mixed}", "kind": "decision", "scope": "repo",
                "text": "Reply then prompt", "actor": "claude:abc123",
                "date": first["date"], "cite": self.ref(self.prompt),
                "quote": "decided to use the zebra",
            },
        )  # fmt: skip
        self.assertNotIn("Reply only", [e["text"] for e in got])
        every = knowledge.block_entries(self.ro(), [self.repo, wide], limit=30)
        self.assertEqual(
            len(every), 12
        )  # 10 + mixed + elsewhere; no reply/call-only
        self.assertEqual(
            len(knowledge.block_entries(self.ro(), [self.repo], limit=3)), 3
        )
        self.assertEqual(knowledge.block_entries(self.ro(), []), [])
        self.assertEqual(
            knowledge.block_entries(self.ro(), [self.repo], limit=0), []
        )

    def test_block_entries_current_only_newest_first_limit(self):
        user = self.cites((self.prompt, "use the zebra cache"))
        a = kid(self.add(text="A", cites=user))
        b = kid(self.add(text="B", cites=user))
        c = kid(self.add(text="C", cites=user))
        d = kid(self.add(text="D", cites=user))
        e = kid(self.add(text="E", cites=user, supersedes=a))  # a: superseded
        knowledge.retract(self.rw, b, reason="wrong", actor="user")
        self.rw.execute(
            "UPDATE knowledge SET text = NULL, status = 'erased' WHERE id = ?",
            (d,),
        )
        wide = kid(self.add(text="G", cites=user, global_scope=True))
        scopes = self.rw.execute(
            "SELECT id FROM scope WHERE key = 'global'"
        ).fetchone()[0]
        got = knowledge.block_entries(self.ro(), [self.repo, scopes])
        self.assertEqual(
            [x["id"] for x in got], [f"K{wide}", f"K{e}", f"K{c}"]
        )
        self.assertEqual(got[0]["scope"], "global")
        self.assertEqual(
            [
                x["id"]
                for x in knowledge.block_entries(
                    self.ro(), [self.repo], limit=1
                )
            ],
            [f"K{e}"],
        )

    def test_the_quote_is_cut_to_120_and_erased_cites_do_not_count(self):
        event = self.add_event(self.src, self.repo, "w" * 200)
        self.add(text="Long quote", cites=self.cites((event, "w" * 150)))
        got = knowledge.block_entries(self.ro(), [self.repo])
        self.assertEqual(got[0]["quote"], "w" * 120)
        stored = self.rw.execute("SELECT length(quote) FROM citation")
        self.assertEqual(stored.fetchone()[0], 150)  # only the block cuts it
        self.rw.execute(
            "UPDATE citation SET quote = NULL, span_start = NULL,"
            " span_end = NULL, state = 'erased'"
        )
        self.assertEqual(knowledge.block_entries(self.ro(), [self.repo]), [])


class RunnerTests(KnowCase):
    """run_add / run_retract: the lock, a fullfsync connection, closed."""

    def kw(self, **extra):
        kw = {
            "kind": "decision", "text": "Use the zebra cache",
            "cites": [(self.ref(self.prompt), "use the zebra cache")],
            "cwd": "/repo", "actor": "claude:abc123", "roots": {}, "env": {},
        }  # fmt: skip
        return kw | extra

    def test_run_add_and_run_retract_lock_write_and_close(self):
        opened = []
        real = store.connect_rw

        def spy(path, *args, **kw):
            opened.append((path, args, kw))
            return real(path, *args, **kw)

        with mock.patch.object(store, "connect_rw", side_effect=spy):
            got = knowledge.run_add(self.home, **self.kw())
            number = int(got["entry"]["id"][1:])
            knowledge.run_retract(
                self.home, kid=number, reason="no", actor="user"
            )
        self.assertEqual([o[0] for o in opened], [self.db, self.db])
        for _, args, kw in opened:
            self.assertTrue(kw.get("fullfsync", True) and not args)  # O4d
        shown = knowledge.show(self.ro(), number)["entry"]
        self.assertEqual(shown["status"], "retracted")
        with store.writer_lock(self.home, wait_s=0):  # both released it
            pass

    def test_a_refusal_still_releases_the_lock(self):
        with self.assertRaises(knowledge.Refused):
            knowledge.run_add(self.home, **self.kw(cites=[]))
        with self.assertRaises(knowledge.Refused):
            knowledge.run_retract(self.home, kid=999, reason="x", actor="user")
        with store.writer_lock(self.home, wait_s=0):
            pass
        self.assertEqual(self.counts()["knowledge"], 0)

    def test_run_add_is_busy_while_another_writer_holds_the_lock(self):
        holder = tst.Child(self, tst.HOLD_LOCK, self.home, 60)
        holder.wait_ready()
        with self.assertRaises(store.Busy):
            knowledge.run_add(self.home, wait_s=0, **self.kw())
        with self.assertRaises(store.Busy):
            knowledge.run_retract(
                self.home, wait_s=0, kid=1, reason="x", actor="user"
            )
        self.assertEqual(self.counts()["knowledge"], 0)


class SearchSeesKnowledgeTests(KnowCase):
    """The entries add() writes are what query.search's knowledge shows."""

    def hits(self, text="zebra"):
        found = tq.query.search(self.ro(), text, cwd="/repo", env={})
        return [(k["id"], k["text"], k["cites"]) for k in found["knowledge"]]

    def test_search_shows_current_entries_until_superseded_or_retracted(self):
        ref = self.ref(self.prompt)
        one = kid(self.add(text="Use the zebra cache"))
        self.assertEqual(
            self.hits(), [(f"K{one}", "Use the zebra cache", [ref])]
        )
        two = kid(
            self.add(text="Use the zebra cache and more", supersedes=one)
        )
        self.assertEqual([h[0] for h in self.hits()], [f"K{two}"])
        knowledge.retract(self.rw, two, reason="wrong", actor="user")
        self.assertEqual(self.hits(), [])
