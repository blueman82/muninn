"""Knowledge verification: citation states, check, erase, reparse."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from typing import Any

from pctx import erase, knowledge
from tests import test_classify as tc
from tests import test_ingest as ti
from tests.knowledge_support import PROMPT, ROOT, KnowCase, kid
from tests.query_support import NOTICE


class VerifyTests(KnowCase):
    """``verify_citation`` and ``check`` against moved or erased lines."""

    def first(self) -> sqlite3.Row:
        """Return the oldest citation row.

        Returns:
            The first row of ``citation``.
        """
        return self.rw.execute("SELECT * FROM citation ORDER BY id").fetchone()

    def state(self) -> str:
        """Verify the oldest citation against the current store.

        Returns:
            The verification state, such as ``ok`` or ``changed``.
        """
        return knowledge.verify_citation(self.ro(), self.first())

    def test_verify_citation_states(self) -> None:
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

    def test_missing_source_and_erased_citation(self) -> None:
        self.add()
        self.rw.execute("UPDATE source SET status = 'missing'")
        self.assertEqual(
            self.state(), "missing"
        )  # the kept event still says so
        self.reparse(self.prompt, digest="h2")
        self.assertEqual(self.state(), "changed")  # a change outranks missing
        self.rw.execute("DELETE FROM event")  # the source row is left
        self.assertEqual(self.state(), "missing")
        self.rw.execute("DELETE FROM source")  # nothing left to look at
        self.assertEqual(self.state(), "missing")
        self.rw.execute("UPDATE citation SET quote = NULL")  # state untouched
        self.assertEqual(self.state(), "erased")
        self.rw.execute(
            "UPDATE citation SET quote = NULL, span_start = NULL,"
            " span_end = NULL, state = 'erased'"
        )
        self.assertEqual(self.state(), "erased")

    def test_check_counts_and_names_the_broken(self) -> None:
        def cite(event: int, quote: str) -> list[tuple[str, str]]:
            """Build a one-element citation list for an event."""
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
        self.assertEqual(got["notice"], NOTICE)
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

    def test_survives_new_process(self) -> None:
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

    def test_erase_marks_citations_and_entries(self) -> None:
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

    def setUp(self) -> None:
        super().setUp()
        self.tid = ti.TID
        self.path = self.write(
            ti.rollout(self.tid), self.records("keep the zebra cache", "noted")
        )
        self.run_ingest()

    def records(self, question: str, answer: str) -> list[dict[str, Any]]:
        """Build the rollout records of one question and its answer.

        Args:
            question: Text of the user prompt.
            answer: Text of the assistant reply.

        Returns:
            Records ready to be written as a rollout file.
        """
        return [
            tc.codex_meta("user", self.tid),
            tc.user_msg(1, question),
            tc.reply(2, answer),
        ]

    def cite(self) -> dict[str, Any]:
        """Add a decision that cites the first prompt of the rollout.

        Returns:
            The result of ``knowledge.add``.
        """
        return knowledge.add(
            self.conn, kind="decision", text="Keep the zebra cache",
            cites=[(f"codex:{self.tid}:2.1", "keep the zebra cache")],
            quote_only=None, supersedes=None, global_scope=False,
            cwd=tc.CWD, actor="user", roots=self.roots, env={},
        )  # fmt: skip

    def test_check_detects_changed_after_reparse(self) -> None:
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

    def test_provider_deleting_the_file_makes_citations_missing(self) -> None:
        self.cite()
        self.path.unlink()
        self.run_ingest()
        got = knowledge.check(self.conn)
        self.assertEqual((got["ok"], got["missing"]), (0, 1))


class EdgeTests(KnowCase):
    """Corner cases of citation storage, check limits and quote choice."""

    def test_duplicate_citations_are_stored_once(self) -> None:
        ref, quote = self.ref(self.prompt), "use the zebra cache"
        got = self.add(
            cites=[
                (ref, quote),
                (ref, "  use   the zebra cache "),
                (ref, quote),
            ]
        )
        self.assertEqual(len(got["entry"]["cites"]), 1)
        two = self.add(cites=[(ref, quote), (ref, "decided to use")])
        self.assertEqual(len(two["entry"]["cites"]), 2)  # other words: kept

    def test_check_names_at_most_100_broken_citations(self) -> None:
        cite = (self.ref(self.prompt), "use the zebra cache")
        for i in range(101):
            self.add(text=f"Entry {i}", cites=[cite])
        self.reparse(self.prompt, digest="h2")
        got = knowledge.check(self.ro())
        self.assertEqual((got["citations"], got["changed"]), (101, 101))
        self.assertEqual(len(got["problems"]), 100)
        self.assertEqual(got["problems_omitted"], 1)

    def test_list_for_an_unknown_cwd_with_no_global_entries_is_empty(
        self,
    ) -> None:
        self.add()
        got = knowledge.list_entries(self.ro(), cwd="/nowhere")
        self.assertEqual((got["count"], got["entries"]), (0, []))
        self.assertEqual(
            knowledge.list_entries(self.ro(), cwd="/repo")["count"], 1
        )

    def test_the_first_user_prompt_quote_is_shown(self) -> None:
        first = (self.ref(self.prompt), "decided to use the zebra")
        second = (self.ref(self.prompt), "for every lookup")
        reply = (self.ref(self.reply), "wire the zebra cache")
        self.add(text="Two quotes", cites=[reply, first, second])
        got = knowledge.block_entries(self.ro(), [self.repo])
        self.assertEqual(got[0]["quote"], "decided to use the zebra")
        self.assertEqual(got[0]["cite"], self.ref(self.prompt))
        for limit in (-1, 0):
            self.assertEqual(
                knowledge.block_entries(self.ro(), [self.repo], limit=limit),
                [],
            )

    def test_quote_only_skips_what_cannot_be_cited(self) -> None:
        text = "we agreed the yak cache is fine for now"

        def prompt(
            name: str,
            ts: str,
            session: str = "sess-x",
            cls: str = "primary",
            **kw: Any,
        ) -> int:
            """Add the same prompt text in a new thread of a given class."""
            src = self.add_source(name, session=session, cls=cls)
            return self.add_event(src, self.repo, text, ts=ts, **kw)

        good = prompt("thr-good", "2026-09-03T10:00:10.000Z")
        prompt("thr-rev", "2026-09-03T10:00:20.000Z", cls="reviewer")
        prompt("thr-sub", "2026-09-03T10:00:30.000Z", cls="subagent")
        prompt("thr-flag", "2026-09-03T10:00:40.000Z", flags=1)
        prompt(
            "thr-reply",
            "2026-09-03T10:00:50.000Z",
            kind="reply",
            role="assistant",
        )
        prompt("thr-harness", "2026-09-03T10:00:55.000Z", kind="harness")
        prompt(
            "thr-delegation",
            "2026-09-03T10:00:58.000Z",
            cls="subagent",
            kind="delegation",
        )
        prompt("thr-other", "2026-09-03T10:01:00.000Z", session="sess-y")
        got = self.add(
            cites=[],
            quote_only="we agreed the yak cache",
            env={"CODEX_SESSION_ID": "sess-x"},
        )
        self.assertEqual(
            [c["ref"] for c in got["entry"]["cites"]], [self.ref(good)]
        )
