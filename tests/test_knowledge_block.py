"""Knowledge in the session block, runners, search and the full lifecycle."""

from __future__ import annotations

import inspect
import os
import sqlite3
from typing import Any
from unittest import mock

from pctx import knowledge, query, store
from tests import test_classify as tc
from tests import test_ingest as ti
from tests import test_store as tst
from tests.knowledge_support import KnowCase, kid


class BlockTests(KnowCase):
    """``block_entries``: what the SessionStart block may push."""

    def cites(self, *pairs: tuple[int, str]) -> list[tuple[str, str]]:
        """Build citation pairs from ``(event, quote)`` pairs.

        Args:
            *pairs: Events with the quote to cite from each.

        Returns:
            ``(ref, quote)`` pairs in the same order.
        """
        return [(self.ref(event), quote) for event, quote in pairs]

    def test_block_entries_user_cited_only_limit8_with_quote(self) -> None:
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

    def test_block_entries_current_only_newest_first_limit(self) -> None:
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

    def test_the_quote_is_cut_to_120_and_erased_cites_do_not_count(
        self,
    ) -> None:
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

    def kw(self, **extra: Any) -> dict[str, Any]:
        """Build ``run_add`` arguments for a repo decision.

        Args:
            **extra: Arguments that override the defaults.

        Returns:
            Keyword arguments for ``knowledge.run_add``.
        """
        kw: dict[str, Any] = {
            "kind": "decision", "text": "Use the zebra cache",
            "cites": [(self.ref(self.prompt), "use the zebra cache")],
            "cwd": "/repo", "actor": "claude:abc123", "roots": {}, "env": {},
        }  # fmt: skip
        return kw | extra

    def test_run_add_and_run_retract_lock_write_and_close(self) -> None:
        opened: list[tuple[Any, ...]] = []
        real = store.connect_rw

        def spy(path: Any, *args: Any, **kw: Any) -> sqlite3.Connection:
            """Open the real connection and record how it was opened."""
            conn = real(path, *args, **kw)
            opened.append((path, args, kw, conn))
            return conn

        with mock.patch.object(store, "connect_rw", side_effect=spy):
            got = knowledge.run_add(self.home, **self.kw())
            number = int(got["entry"]["id"][1:])
            knowledge.run_retract(
                self.home, kid=number, reason="no", actor="user"
            )
        self.assertEqual([o[0] for o in opened], [self.db, self.db])
        for _, args, kw, conn in opened:
            # durability: the default full fsync must not be switched off
            self.assertTrue(kw.get("fullfsync", True) and not args)
            with self.assertRaises(sqlite3.ProgrammingError):  # closed
                conn.execute("SELECT 1")
        shown = knowledge.show(self.ro(), number)["entry"]
        self.assertEqual(shown["status"], "retracted")
        with store.writer_lock(self.home, wait_s=0):  # both released it
            pass
        for run in (knowledge.run_add, knowledge.run_retract):
            waits = inspect.signature(run).parameters["wait_s"]
            self.assertEqual(waits.default, 15.0)

    def test_a_refusal_still_releases_the_lock(self) -> None:
        with self.assertRaises(knowledge.RefusedError):
            knowledge.run_add(self.home, **self.kw(cites=[]))
        with self.assertRaises(knowledge.RefusedError):
            knowledge.run_retract(self.home, kid=999, reason="x", actor="user")
        with store.writer_lock(self.home, wait_s=0):
            pass
        self.assertEqual(self.counts()["knowledge"], 0)

    def test_run_add_is_busy_while_another_writer_holds_the_lock(self) -> None:
        holder = tst.Child(self, tst.HOLD_LOCK, self.home, 60)
        holder.wait_ready()
        with self.assertRaises(store.BusyError):
            knowledge.run_add(self.home, wait_s=0, **self.kw())
        with self.assertRaises(store.BusyError):
            knowledge.run_retract(
                self.home, wait_s=0, kid=1, reason="x", actor="user"
            )
        self.assertEqual(self.counts()["knowledge"], 0)


class SearchSeesKnowledgeTests(KnowCase):
    """The entries add() writes are what query.search's knowledge shows."""

    def hits(self, text: str = "zebra") -> list[tuple[str, str, Any]]:
        """Search the repo and return its knowledge hits.

        Args:
            text: Search text.

        Returns:
            ``(id, text, cites)`` for every knowledge hit.
        """
        found = query.search(self.ro(), text, cwd="/repo", env={})
        return [(k["id"], k["text"], k["cites"]) for k in found["knowledge"]]

    def test_search_shows_current_entries_until_superseded_or_retracted(
        self,
    ) -> None:
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


class StoreTroubleTests(KnowCase):
    """An unreadable store raises StoreUnavailableError, not sqlite errors.

    Every public reader and writer must map it (the CLI exits 4).
    """

    def test_store_lost_mid_call(self) -> None:
        self.add()
        reader = self.ro()
        cites = [(self.ref(self.prompt), "use the zebra cache")]
        citation = reader.execute("SELECT * FROM citation").fetchone()
        calls = {
            "list_entries": lambda: knowledge.list_entries(
                reader, cwd="/repo"
            ),
            "show": lambda: knowledge.show(reader, 1),
            "check": lambda: knowledge.check(reader),
            "verify_citation": lambda: knowledge.verify_citation(
                reader, citation
            ),
            "block_entries": lambda: knowledge.block_entries(
                reader, [self.repo]
            ),
            "add": lambda: knowledge.add(
                self.rw,
                kind="decision",
                text="Again",
                cites=cites,
                cwd="/repo",
                actor="user",
                roots={},
                env={},
            ),
            "retract": lambda: knowledge.retract(
                self.rw, 1, reason="x", actor="user"
            ),
        }
        self.db.write_bytes(os.urandom(8192))  # no database any more
        for name, call in calls.items():
            with (
                self.subTest(name),
                self.assertRaises(store.StoreUnavailableError),
            ):
                call()


class LifecycleTests(ti.IngestCase):
    """The flow on real ingest output: cite, find, correct, refuse."""

    def setUp(self) -> None:
        super().setUp()
        self.tid = ti.TID
        self.write(
            ti.rollout(self.tid),
            [
                tc.codex_meta("user", self.tid),
                tc.user_msg(1, "the canary build flag is off for now"),
                tc.reply(2, "noted"),
            ],
        )
        main = f"-work-repo/{tc.SESSION}.jsonl"
        self.write(
            main,
            [tc.claude_rec("user", "correction: the canary build flag is on")],
            root="claude-projects",
        )
        self.run_ingest()

    def add(self, **kw: Any) -> dict[str, Any]:
        """Call ``knowledge.add`` on the ingested store with defaults.

        Args:
            **kw: Arguments that override the defaults.

        Returns:
            The result of ``knowledge.add``.
        """
        args: dict[str, Any] = {
            "kind": "fact", "text": "The build flag is off", "cites": [],
            "cwd": tc.CWD, "actor": "user", "roots": self.roots, "env": {},
        }  # fmt: skip
        return knowledge.add(self.conn, **(args | kw))

    def test_cite_find_correct_and_refuse(self) -> None:
        codex = f"codex:{self.tid}:2.1"
        claude = f"claude:{tc.SESSION}:1.1"
        one = self.add(cites=[(codex, "build flag is off")])["entry"]
        found = query.search(self.conn, "build flag", cwd=tc.CWD, env={})
        self.assertEqual([k["id"] for k in found["knowledge"]], [one["id"]])
        self.assertEqual(found["knowledge"][0]["cites"], [codex])
        opened = query.open_event(self.conn, codex, roots=self.roots, raw=True)
        self.assertTrue(opened["hash_ok"])  # the citation opens the original
        two = self.add(
            text="The build flag is on",
            cites=[(claude, "build flag is on")],
            supersedes=one["id"],
        )["entry"]
        found = query.search(self.conn, "build flag", cwd=tc.CWD, env={})
        self.assertEqual([k["id"] for k in found["knowledge"]], [two["id"]])
        history = knowledge.show(self.conn, one["id"])
        self.assertEqual(history["entry"]["text"], "The build flag is off")
        self.assertEqual(history["entry"]["status"], "superseded")
        self.assertEqual(history["entry"]["cites"][0]["ref"], codex)
        self.assertEqual(
            [e["id"] for e in history["chain"]["superseded_by"]], [two["id"]]
        )
        for cites, code in (
            ([], "uncited"),
            ([(f"codex:{self.tid}:99.1", "a made up citation")], "not_found"),
            ([(codex, "a quote that is not there")], "quote_not_found"),
        ):
            with (
                self.subTest(code),
                self.assertRaises(knowledge.RefusedError) as c,
            ):
                self.add(cites=cites)
            self.assertEqual(c.exception.code, code)
