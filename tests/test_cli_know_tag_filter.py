"""muninn know list --tag: AND semantics over whole tag elements."""

from __future__ import annotations

from tests.cli_support import CliCase
from tests.test_ingest import TID


class TagFilterTests(CliCase):
    """``--tag`` narrows the list and the count follows the filter."""

    QUOTE = "the zebra cache stays in place"

    def setUp(self) -> None:
        super().setUp()
        self.session(TID, f"we decided {self.QUOTE} for good", "noted")
        self.run_ingest()
        self.ref = f"codex:{TID}:2.1"
        for text, kind, tags in (
            ("one", "lesson", ("run-1", "alpha")),
            ("ten", "fact", ("run-10",)),
            ("both", "lesson", ("run-1", "run-10")),
            ("bare", "lesson", ()),
            ("under", "lesson", ("a_b",)),
        ):
            self.add(text, kind, tags)

    def add(
        self, text: str, kind: str, tags: tuple[str, ...], *extra: str
    ) -> None:
        """Add one cited entry with ``tags`` and any ``extra`` flags."""
        flags = [f for t in tags for f in ("--tag", t)]
        code, out, _ = self.muninn(
            "know", "add", "--kind", kind, "--text", text,
            "--cite", self.ref, "--quote", self.QUOTE, *flags, *extra,
        )  # fmt: skip
        self.assertEqual(code, 0, out)

    def texts(self, *flags: str) -> list[str]:
        """Return the sorted entry texts of ``know list`` with ``flags``."""
        code, out, _ = self.muninn("know", "list", *flags)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["count"], len(out["entries"]))
        return sorted(e["text"] for e in out["entries"])

    def test_single_tag_matches_whole_elements_only(self) -> None:
        self.assertEqual(self.texts("--tag", "run-1"), ["both", "one"])
        self.assertEqual(self.texts("--tag", "run-10"), ["both", "ten"])
        self.assertEqual(self.texts("--tag", "run"), [])
        self.assertEqual(self.texts("--tag", "a_b"), ["under"])
        self.assertEqual(self.texts("--tag", "axb"), [])

    def test_repeated_tags_are_anded_in_any_order(self) -> None:
        want = ["both"]
        self.assertEqual(self.texts("--tag", "run-1", "--tag", "run-10"), want)
        self.assertEqual(self.texts("--tag", "run-10", "--tag", "run-1"), want)
        self.assertEqual(
            self.texts("--tag", "run-1", "--tag", "run-1"), ["both", "one"]
        )

    def test_no_match_counts_zero(self) -> None:
        self.assertEqual(self.texts("--tag", "alpha", "--tag", "run-10"), [])

    def test_composes_with_kind_status_and_loop(self) -> None:
        self.assertEqual(
            self.texts("--tag", "run-10", "--kind", "fact"), ["ten"]
        )
        self.assertEqual(
            self.texts("--tag", "run-10", "--kind", "lesson"), ["both"]
        )
        self.assertEqual(
            self.texts("--tag", "run-1", "--status", "all"), ["both", "one"]
        )
        self.assertEqual(
            self.texts("--tag", "run-1", "--status", "retracted"), []
        )
        self.add("looped", "fact", ("run-1", "x"), "--scope-loop", "loop-7")
        self.assertEqual(
            self.texts("--scope-loop", "loop-7", "--tag", "x"), ["looped"]
        )
        self.assertEqual(
            self.texts("--scope-loop", "loop-7", "--tag", "alpha"), []
        )

    def test_bad_tag_refused_like_add(self) -> None:
        for flags in (("--tag", "Bad"), ("--tag", "a,b"), ("--tag", "")):
            with self.subTest(flags):
                code, out, _ = self.muninn("know", "list", *flags)
                self.assertEqual((code, out["error"]), (2, "bad_tags"))
        many = [f for n in range(11) for f in ("--tag", f"t{n}")]
        code, out, _ = self.muninn("know", "list", *many)
        self.assertEqual((code, out["error"]), (2, "bad_tags"))

    def test_without_tag_nothing_changes(self) -> None:
        self.assertEqual(len(self.texts()), 5)
