"""Knowledge ledger: supersede chains, retraction, list and show."""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from pctx import knowledge
from tests import test_query as tq
from tests.knowledge_support import SECRET, KnowCase, kid


class LedgerTests(KnowCase):
    """Entries change state only through supersede and retract."""

    def log(self, kind_id: int) -> list[tuple[Any, ...]]:
        """Return the ``(action, actor)`` log rows of one entry in order.

        Args:
            kind_id: Primary key of the knowledge entry.

        Returns:
            The log rows, oldest first.
        """
        return [
            tuple(r)
            for r in self.rw.execute(
                "SELECT action, actor FROM knowledge_log"
                " WHERE knowledge_id = ? ORDER BY id",
                (kind_id,),
            )
        ]

    def row(self, kind_id: int) -> sqlite3.Row:
        """Return the knowledge row of one entry.

        Args:
            kind_id: Primary key of the knowledge entry.

        Returns:
            The full row.
        """
        return self.rw.execute(
            "SELECT * FROM knowledge WHERE id = ?", (kind_id,)
        ).fetchone()

    def test_supersede_chain_and_log(self) -> None:
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

    def test_supersede_scope_mismatch_refused(self) -> None:
        one = kid(self.add())
        self.add_scope("/other")
        self.refused("bad_supersedes", supersedes=one, global_scope=True)
        self.refused("bad_supersedes", supersedes=one, cwd="/other")
        wide = kid(self.add(global_scope=True))
        self.refused("bad_supersedes", supersedes=wide)  # a repo entry can't
        self.add(supersedes=wide, global_scope=True)
        self.assertEqual(self.row(wide)["status"], "superseded")
        self.assertEqual(self.row(one)["status"], "current")

    def test_supersede_needs_a_current_entry(self) -> None:
        one = kid(self.add())
        two = kid(self.add(supersedes=one))
        gone = kid(self.add())
        knowledge.retract(self.rw, gone, reason="wrong", actor="user")
        targets: tuple[Any, ...] = (one, gone, 999, "K999", "junk", 0, -1)
        for target in targets:
            with self.subTest(target):
                self.refused("bad_supersedes", supersedes=target)
        self.assertEqual(self.row(two)["status"], "current")

    def test_retract(self) -> None:
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
        bad_ids: tuple[tuple[Any, str], ...] = (
            (one, "not_current"),  # already retracted
            (999, "not_found"),
            ("junk", "not_found"),
        )
        for bad, code in bad_ids:
            with (
                self.subTest(bad),
                self.assertRaises(knowledge.RefusedError) as caught,
            ):
                knowledge.retract(
                    self.rw, cast("Any", bad), reason="x", actor="user"
                )
            self.assertEqual(caught.exception.code, code)
        old = kid(self.add())
        self.add(supersedes=old)
        with self.assertRaises(knowledge.RefusedError) as caught:  # superseded
            knowledge.retract(self.rw, old, reason="x", actor="user")
        self.assertEqual(caught.exception.code, "not_current")
        fresh = kid(self.add())
        with self.assertRaises(knowledge.RefusedError) as caught:
            knowledge.retract(self.rw, fresh, reason="r" * 201, actor="user")
        self.assertEqual(caught.exception.code, "reason_length")
        with self.assertRaises(knowledge.RefusedError) as caught:
            knowledge.retract(self.rw, fresh, reason="ok", actor="")
        self.assertEqual(caught.exception.code, "bad_actor")
        self.assertEqual(self.row(fresh)["status"], "current")
        knowledge.retract(self.rw, fresh, reason="", actor="user")  # optional
        self.assertIsNone(self.row(fresh)["retract_reason"])


class ListShowTests(KnowCase):
    """``list_entries`` and ``show`` over a mixed set of entries."""

    def setUp(self) -> None:
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

    def ids(self, **kw: Any) -> list[int]:
        """List entry ids for ``/repo`` with the given filters.

        Args:
            **kw: Arguments that override the ``list_entries`` defaults.

        Returns:
            Numeric entry ids in listing order.
        """
        args: dict[str, Any] = {"cwd": "/repo"} | kw
        got = knowledge.list_entries(self.ro(), **args)
        return [int(e["id"][1:]) for e in got["entries"]]

    def test_list_entries_scope_status_kind_order(self) -> None:
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

    def test_list_entries_carry_actor_date_and_citation_states(self) -> None:
        got = knowledge.list_entries(self.ro(), cwd="/repo")
        self.assertEqual((got["notice"], got["count"]), (tq.NOTICE, 3))
        entry = got["entries"][-1]  # the oldest: A
        self.assertEqual(entry["id"], f"K{self.a}")
        self.assertEqual(entry["actor"], "claude:abc123")
        self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual([c["verify"] for c in entry["cites"]], ["ok"])

    def test_show_ids_and_unknown(self) -> None:
        ro = self.ro()
        forms: tuple[Any, ...] = (
            self.new,
            f"K{self.new}",
            f"k{self.new}",
            str(self.new),
        )
        for ref in forms:
            with self.subTest(ref):
                self.assertEqual(
                    knowledge.show(ro, ref)["entry"]["id"], f"K{self.new}"
                )
        unknown: tuple[Any, ...] = (999, "junk", "K", None, True, False)
        for ref in (*unknown, "9" * 30):
            with self.subTest(ref):
                self.assertEqual(
                    knowledge.show(ro, cast("Any", ref)),
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
