"""pctx know add, retract, list, show and check through main()."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

from pctx import cli_core, ingest, knowledge, store
from tests.cli_support import ALWAYS, CliCase
from tests.test_classify import user_msg
from tests.test_ingest import TID, rollout


class KnowTests(CliCase):
    """pctx know add|retract|list|show|check through main()."""

    QUOTE = "the zebra cache stays in place"

    def setUp(self) -> None:
        super().setUp()
        self.session(TID, f"we decided {self.QUOTE} for good", "noted")
        self.run_ingest()
        self.ref = f"codex:{TID}:2.1"

    def add(
        self,
        *extra: str,
        text: str = "Keep the zebra cache",
        kind: str = "decision",
        env: dict[str, str] | None = None,
    ) -> tuple[int, Any, str]:
        """Run ``pctx know add`` with the given text and kind.

        Args:
            *extra: Further flags, such as ``--cite`` and ``--quote``.
            text: Entry text.
            kind: Entry kind.
            env: Variables layered over the test environment.

        Returns:
            Exit code, parsed JSON and stderr of the call.
        """
        return self.pctx(
            "know", "add", "--kind", kind, "--text", text, *extra, env=env
        )

    def test_know_add_end_to_end_with_a_verbatim_quote(self) -> None:
        code, out, _ = self.add("--cite", self.ref, "--quote", self.QUOTE)
        self.assertEqual(code, 0, out)
        self.assertLessEqual(ALWAYS | {"entry"}, set(out))
        entry = out["entry"]
        self.assertEqual(
            (entry["id"], entry["status"], entry["kind"], entry["actor"]),
            ("K1", "current", "decision", "user"),
        )
        self.assertEqual(
            [(c["ref"], c["quote"], c["verify"]) for c in entry["cites"]],
            [(self.ref, self.QUOTE, "ok")],
        )
        code, listed, _ = self.pctx("know", "list")
        self.assertEqual((code, listed["count"]), (0, 1))
        self.assertLessEqual(ALWAYS | {"entries", "count"}, set(listed))
        code, shown, _ = self.pctx("know", "show", "K1")
        self.assertEqual(code, 0)
        self.assertLessEqual(ALWAYS | {"entry", "chain", "log"}, set(shown))
        self.assertEqual([x["action"] for x in shown["log"]], ["add"])
        code, checked, _ = self.pctx("know", "check")
        self.assertEqual(
            (code, checked["ok"], checked["problems"]), (0, 1, [])
        )
        code, gone, _ = self.pctx(
            "know", "retract", "K1", "--reason", "changed"
        )
        self.assertEqual((code, gone["entry"]["status"]), (0, "retracted"))
        _, after, _ = self.pctx("know", "list")
        self.assertEqual(after["count"], 0)
        _, every, _ = self.pctx("know", "list", "--status", "all")
        self.assertEqual(every["count"], 1)
        self.assertEqual(self.pctx("know", "list", "--status", "old")[0], 2)

    def test_know_add_refusals_exit_2(self) -> None:
        cases = (
            ((), "uncited"),
            (
                ("--cite", self.ref, "--quote", "a quote nobody typed"),
                "quote_not_found",
            ),
            (
                ("--cite", f"codex:{TID}:99.1", "--quote", self.QUOTE),
                "not_found",
            ),
            (("--cite", "garbage", "--quote", self.QUOTE), "bad_ref"),
            (("--cite", self.ref, "--quote", "we decided"), "quote_length"),
        )
        for extra, error in cases:
            with self.subTest(error):
                code, out, _ = self.add(*extra)
                self.assertEqual((code, out["error"]), (2, error))
                self.assertLessEqual(ALWAYS, set(out))
        code, out, _ = self.add(
            "--cite",
            self.ref,
            "--quote",
            self.QUOTE,
            kind="preference",
            text="x" * 501,
        )
        self.assertEqual((code, out["error"]), (2, "text_length"))
        self.assertEqual(
            self.pctx("know", "list")[1]["count"], 0
        )  # nothing written
        self.assertEqual(self.add(kind="rumor")[0], 2)  # argparse: bad choice

    def test_cite_and_quote_pairing(self) -> None:
        two = f"codex:{TID}:3.1"
        bad = ("--cite", self.ref, "--quote", "we decided the zebra cache")
        bad += ("--cite", two, "--quote", "noted is one word")
        code, out, _ = self.add(*bad)
        self.assertEqual((code, out["error"]), (2, "quote_not_found"))
        for extra in (
            ("--cite", self.ref),  # a cite without its quote
            ("--cite", self.ref, "--cite", two, "--quote", "we decided"),
            ("--quote", "one quote alone", "--quote", "and another alone"),
        ):
            with self.subTest(extra):
                code, out, _ = self.add(*extra)
                self.assertEqual((code, out["error"]), (2, "refused"))
        self.assertEqual(self.pctx("know", "list")[1]["count"], 0)
        good = ("--cite", self.ref, "--quote", "we decided the zebra")
        good += ("--cite", self.ref, "--quote", "cache stays in place for")
        code, out, _ = self.add(*good)
        self.assertEqual(code, 0, out)
        self.assertEqual(
            [c["quote"] for c in out["entry"]["cites"]],
            ["we decided the zebra", "cache stays in place for"],
        )

    def test_quote_alone_cites_the_callers_fresh_prompt(self) -> None:
        path = self.roots["codex-sessions"] / rollout(TID)
        self.append(path, [user_msg(5, f"final: {self.QUOTE}, ship it")])
        env = {"CODEX_THREAD_ID": TID}
        code, out, _ = self.add("--quote", f"final: {self.QUOTE}", env=env)
        self.assertEqual(code, 0, out)
        cite = out["entry"]["cites"][0]
        self.assertEqual(cite["ref"], f"codex:{TID}:4.1")
        self.assertEqual(out["entry"]["actor"], f"codex:{TID[:12]}")
        code, out, _ = self.add(
            "--quote", f"final: {self.QUOTE}"
        )  # no session
        self.assertEqual((code, out["error"]), (2, "no_caller_session"))

    def test_supersede_and_global(self) -> None:
        self.add("--cite", self.ref, "--quote", self.QUOTE)
        code, out, _ = self.add(
            "--cite",
            self.ref,
            "--quote",
            self.QUOTE,
            "--supersedes",
            "K1",
            text="Keep it, revised",
        )
        self.assertEqual((code, out["entry"]["supersedes"]), (0, "K1"))
        _, shown, _ = self.pctx("know", "show", "1")
        self.assertEqual(shown["entry"]["status"], "superseded")
        self.assertEqual(
            [c["id"] for c in shown["chain"]["superseded_by"]], ["K2"]
        )
        code, out, _ = self.add(
            "--cite", self.ref, "--quote", self.QUOTE, "--supersedes", "K1"
        )
        self.assertEqual((code, out["error"]), (2, "bad_supersedes"))
        code, out, _ = self.add(
            "--cite", self.ref, "--quote", self.QUOTE, "--global"
        )
        self.assertEqual((code, out["entry"]["scope"]), (0, "global"))
        _, here, _ = self.pctx("know", "list")
        _, wide, _ = self.pctx("know", "list", "--all-projects")
        self.assertEqual((here["count"], wide["count"]), (2, 2))

    def test_know_list_all_projects_reaches_other_scopes(self) -> None:
        self.add("--cite", self.ref, "--quote", self.QUOTE)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        knowledge.run_add(
            self.home,
            kind="fact",
            text="Kept in another project",
            cites=[(self.ref, self.QUOTE)],
            cwd=str(elsewhere),
            actor="user",
            roots=ingest.default_roots(self.env),
            env=self.env,
        )
        _, here, _ = self.pctx("know", "list")
        _, wide, _ = self.pctx("know", "list", "--all-projects")
        self.assertEqual([e["id"] for e in here["entries"]], ["K1"])
        self.assertEqual(
            sorted(e["id"] for e in wide["entries"]), ["K1", "K2"]
        )

    def test_show_unknown_and_reader_failures(self) -> None:
        self.assertEqual(
            self.pctx("know", "show", "K9")[1]["error"], "not_found"
        )
        self.assertEqual(self.pctx("know", "show", "K9")[0], 2)
        empty = {"PCTX_HOME": str(self.tmp / "empty")}
        for argv in (("list",), ("show", "K1"), ("check",)):
            with self.subTest(argv):
                code, out, _ = self.pctx("know", *argv, env=empty)
                self.assertEqual(
                    (code, out["error"]), (4, "store_unavailable")
                )

    def test_know_writers_are_busy_while_the_lock_is_held(self) -> None:
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(cli_core, "WRITER_WAIT_S", 0),
        ):
            code, out, _ = self.add("--cite", self.ref, "--quote", self.QUOTE)
            self.assertEqual((code, out["error"]), (3, "busy"))
            code, out, _ = self.pctx("know", "retract", "K1", "--reason", "x")
            self.assertEqual((code, out["error"]), (3, "busy"))

    def test_know_calls_log_ids_not_text(self) -> None:
        self.add("--cite", self.ref, "--quote", self.QUOTE)
        self.pctx("know", "list")
        self.add()  # refused
        lines = [
            json.loads(x)
            for x in (self.home / "calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [x["cmd"] for x in lines], ["know add", "know list", "know add"]
        )
        self.assertEqual(lines[0]["knowledge_ids"], [1])
        self.assertEqual(lines[2]["error"], "uncited")
        blob = (self.home / "calls.jsonl").read_text()
        for needle in ("zebra", "decided", "Keep the"):
            self.assertNotIn(needle, blob)
