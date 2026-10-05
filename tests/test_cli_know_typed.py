"""muninn know add with the typed ledger fields, through main()."""

from __future__ import annotations

import time
from typing import Any

from muninn import store
from tests.cli_support import ALWAYS, CliCase
from tests.test_ingest import TID


class TypedAddTests(CliCase):
    """Every typed flag is optional, validated and round-trips."""

    QUOTE = "the zebra cache stays in place"

    def setUp(self) -> None:
        super().setUp()
        self.session(TID, f"we decided {self.QUOTE} for good", "noted")
        self.run_ingest()
        self.ref = f"codex:{TID}:2.1"

    def add(
        self, *extra: str, kind: str = "lesson", text: str = "Keep the cache"
    ) -> tuple[int, Any, str]:
        """Run ``know add`` with a citation plus ``extra`` flags."""
        return self.muninn(
            "know", "add", "--kind", kind, "--text", text,
            "--cite", self.ref, "--quote", self.QUOTE, *extra,
        )  # fmt: skip

    def test_every_flag_round_trips_through_add_list_and_show(self) -> None:
        self.assertEqual(self.add(kind="fact", text="base")[0], 0)
        code, out, _ = self.add(
            "--confidence", "observed",
            "--valid-until", "2099-01-02",
            "--sensitivity", "restricted",
            "--contradicts", "K1",
            "--tag", "b-tag", "--tag", "a_tag", "--tag", "b-tag",
        )  # fmt: skip
        self.assertEqual(code, 0, out)
        want = {
            "kind": "lesson",
            "confidence": "observed",
            "valid_until": "2099-01-02T00:00:00Z",
            "sensitivity": "restricted",
            "contradicts": "K1",
            "tags": ["a_tag", "b-tag"],
            "expired": False,
        }
        entry = out["entry"]
        self.assertEqual({k: entry[k] for k in want}, want)
        _, listed, _ = self.muninn("know", "list")
        _, shown, _ = self.muninn("know", "show", "K2")
        self.assertLessEqual(ALWAYS | {"entries", "count"}, set(listed))
        self.assertLessEqual(ALWAYS | {"entry", "chain", "log"}, set(shown))
        self.assertEqual({k: shown["entry"][k] for k in want}, want)
        self.assertEqual(listed["count"], 2)
        self.assertEqual(
            [x["action"] for x in shown["log"]], ["add"]
        )  # contradicts never adds a log action

    def test_contradicts_never_changes_the_other_entry(self) -> None:
        self.add(kind="fact", text="base")
        self.add("--contradicts", "K1")
        _, shown, _ = self.muninn("know", "show", "K1")
        self.assertEqual(shown["entry"]["status"], "current")

    def test_constraint_kind_and_old_invocations_unchanged(self) -> None:
        code, out, _ = self.add(kind="constraint")
        self.assertEqual(code, 0, out)
        entry = out["entry"]
        self.assertEqual(
            (
                entry["confidence"],
                entry["valid_until"],
                entry["sensitivity"],
                entry["contradicts"],
                entry["tags"],
            ),
            (None, None, "normal", None, []),
        )

    def test_loop_scope_round_trips_and_stays_out_of_the_repo_list(
        self,
    ) -> None:
        self.assertEqual(self.add("--scope-loop", "loop-7")[0], 0)
        self.assertEqual(self.add(text="repo one")[0], 0)
        _, listed, _ = self.muninn("know", "list", "--scope-loop", "loop-7")
        self.assertEqual(
            [(e["text"], e["scope"]) for e in listed["entries"]],
            [("Keep the cache", "loop:loop-7")],
        )
        _, plain, _ = self.muninn("know", "list")
        self.assertEqual([e["text"] for e in plain["entries"]], ["repo one"])

    def test_bad_values_refuse_with_stable_codes(self) -> None:
        cases = {
            "bad_confidence": ("--confidence", "sure"),
            "bad_sensitivity": ("--sensitivity", "secret"),
            "bad_valid_until": ("--valid-until", "tomorrow"),
            "bad_tags": ("--tag", "Bad Tag"),
            "bad_contradicts": ("--contradicts", "K99"),
            "bad_loop_scope": ("--scope-loop", "no/slash"),
        }
        for code_name, flags in cases.items():
            with self.subTest(code_name):
                code, out, _ = self.add(*flags)
                self.assertEqual((code, out["error"]), (2, code_name))
        _, listed, _ = self.muninn("know", "list", "--status", "all")
        self.assertEqual(listed["count"], 0)

    def test_scope_loop_with_all_projects_refused(self) -> None:
        code, out, _ = self.muninn(
            "know", "list", "--scope-loop", "x", "--all-projects"
        )
        self.assertEqual((code, out["error"]), (2, "loop_with_all_projects"))

    def test_past_valid_until_and_loop_with_global_refused(self) -> None:
        code, out, _ = self.add("--valid-until", "2001-01-01")
        self.assertEqual((code, out["error"]), (2, "bad_valid_until"))
        code, out, _ = self.add("--scope-loop", "x", "--global")
        self.assertEqual((code, out["error"]), (2, "bad_loop_scope"))

    def loop_list(self, *extra: str) -> tuple[int, Any]:
        """Run ``know list --scope-loop loop-7`` with extra flags."""
        code, out, _ = self.muninn(
            "know", "list", "--scope-loop", "loop-7", *extra
        )
        return code, out

    def test_unknown_loop_is_flagged_not_just_empty(self) -> None:
        code, out = self.loop_list()
        self.assertEqual((code, out["count"], out["entries"]), (0, 0, []))
        self.assertIs(out["loop_not_found"], True)
        self.assertEqual(self.add("--scope-loop", "loop-7")[0], 0)
        code, out = self.loop_list()
        self.assertEqual((code, out["count"]), (0, 1))
        self.assertNotIn("loop_not_found", out)

    def test_loop_list_honours_status_and_kind(self) -> None:
        self.assertEqual(
            self.add("--scope-loop", "loop-7", kind="fact", text="a fact")[0],
            0,
        )
        self.assertEqual(
            self.add("--scope-loop", "loop-7", text="a lesson")[0], 0
        )
        conn = store.connect_rw(store.db_path(self.home), fullfsync=False)
        conn.execute(
            "UPDATE knowledge SET valid_until = ? WHERE text = 'a lesson'",
            (time.time() - 5,),
        )
        conn.close()
        for flags, want in (
            (("--status", "expired"), ["a lesson"]),
            (("--status", "all"), ["a lesson", "a fact"]),
            (("--status", "all", "--kind", "fact"), ["a fact"]),
            ((), ["a fact"]),
        ):
            with self.subTest(flags):
                _, out = self.loop_list(*flags)
                self.assertEqual([e["text"] for e in out["entries"]], want)
                self.assertNotIn("loop_not_found", out)
