"""muninn know add with the typed ledger fields, through main()."""

from __future__ import annotations

from typing import Any

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

    def test_past_valid_until_and_loop_with_global_refused(self) -> None:
        code, out, _ = self.add("--valid-until", "2001-01-01")
        self.assertEqual((code, out["error"]), (2, "bad_valid_until"))
        code, out, _ = self.add("--scope-loop", "x", "--global")
        self.assertEqual((code, out["error"]), (2, "bad_loop_scope"))
