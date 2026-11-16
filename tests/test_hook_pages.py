"""Check the session-start list layout and its continuation pages."""

from __future__ import annotations

import json
import re
import time
import unittest
from unittest import mock

from muninn import hook, hook_start, knowledge_push, knowledge_write
from muninn.hook_banner import Item, banner
from muninn.hook_frame import CLOSE, OPEN, USAGE
from muninn.knowledge_models import KnowledgeContext
from tests.hook_support import HookCase
from tests.test_cli_learning import CliLearningCase

CODE = re.compile(r"^▓ (\d+): (K_[0-9a-f]{32})$", re.M)
MORE = re.compile(
    r"^▓ (\d+) more memories\. Before you write code, run: muninn know"
    r" context --page (\d+)",
    re.M,
)


class BannerTests(unittest.TestCase):
    """Size the bars and wrap the memory text."""

    def test_bar_width_is_the_longest_line_from_20_to_72(self) -> None:
        short = banner("▓ M", [Item("K_1", "a", ())], None)
        self.assertEqual(short[0], "▓" * 20)
        self.assertEqual(short[-1], short[0])
        long = banner("▓ " + "h" * 90, [Item("K_1", "word " * 40, ())], None)
        self.assertEqual(long[0], "▓" * 72)
        wrapped = banner("▓ M", [Item("K_1", "word " * 40, ())], None)
        self.assertEqual(wrapped[0], "▓" * max(map(len, wrapped[1:-1])))
        middle = banner("▓ " + "h" * 38, [], None)
        self.assertEqual(middle[0], "▓" * 40)

    def test_text_wraps_at_70_with_three_space_indent(self) -> None:
        lines = banner(
            "▓ M", [Item("K_1", "word " * 40, ("only on Mondays",))], None
        )
        body = lines[3 : lines.index("", 3)]
        self.assertTrue(body[0].startswith("1. word"))
        self.assertTrue(all(len(line) <= 70 for line in body))
        self.assertTrue(all(line.startswith("   ") for line in body[1:]))
        self.assertEqual(body[-1], "   When: only on Mondays")
        self.assertEqual(lines[-2], "▓ 1: K_1")


class PageTests(HookCase):
    """Split every memory into pages, each one once."""

    def setUp(self) -> None:
        super().setUp()
        self.ids = [
            self.add(
                content=f"Memory {i}: " + "detail " * 60, created_at=20.0 + i
            )
            for i in range(12)
        ]

    def everything(self) -> KnowledgeContext:
        """Read every eligible memory without a byte cap."""
        return knowledge_push.knowledge_context(
            self.conn, self.scope, None, time.time()
        )

    def test_pages_cover_every_memory_once_per_provider(self) -> None:
        context = self.everything()
        newest = list(reversed(self.ids))
        for provider, limit, flag in (
            ("claude", 4000, ""),
            ("codex", 2800, " --provider codex"),
        ):
            with self.subTest(provider=provider):
                start = self.body(self.start_hook(provider))
                self.assertLessEqual(len(start.encode()), limit)
                first = [m[2] for m in CODE.finditer(start)]
                seen: list[str] = []
                number = 1
                while True:
                    page = hook_start.render_page(
                        context, "repo", number, limit=limit, flag=flag
                    )
                    self.assertTrue(page.startswith(OPEN))
                    self.assertTrue(page.endswith(CLOSE))
                    self.assertLessEqual(len(page.encode()), limit)
                    codes = [m[2] for m in CODE.finditer(page)]
                    seen += codes
                    more = MORE.search(page)
                    if more is None:
                        break
                    self.assertEqual(
                        (int(more[1]), int(more[2])),
                        (len(newest) - len(seen), number + 1),
                    )
                    self.assertIn(flag + "\n", page[more.start() :])
                    number += 1
                self.assertEqual(seen, newest)
                self.assertEqual(seen[: len(first)], first)
                self.assertGreater(number, 1)
                self.assertIn(f"page {number} of {number} ·", page)
                self.assertIn(
                    f"▓ MUNINN MEMORY · repo · {len(first)} of 12", start
                )
                with self.assertRaisesRegex(ValueError, "page_unavailable"):
                    hook_start.render_page(
                        context, "repo", number + 1, limit=limit
                    )

    def test_codex_pages_are_smaller(self) -> None:
        context = self.everything()
        sizes = [
            len(
                hook_start.start_pages(context, "repo", limit, "")[0],
            )
            for limit in (hook.BLOCK_LIMIT, hook.CODEX_BLOCK_LIMIT)
        ]
        self.assertGreater(sizes[0], sizes[1])


class PageCliTests(CliLearningCase):
    """Print a page as text, not JSON."""

    def test_know_context_page_prints_the_list(self) -> None:
        code, receipt = self.propose()
        self.assertEqual(code, 0)
        ident = receipt["applied"][0]["knowledge_id"]
        with self.assertRaises(json.JSONDecodeError):
            self.call("know", "context", "--page", "1")
        page = self.last_stdout
        self.assertTrue(page.startswith(OPEN))
        self.assertTrue(page.rstrip("\n").endswith(CLOSE))
        self.assertIn(USAGE, page)
        self.assertIn("· page 1 of 1 · 1 of 1", page)
        self.assertIn(f"▓ 1: {ident}", page)
        self.assertNotIn("more memories", page)
        code, out = self.call("know", "context", "--page", "2")
        self.assertEqual((code, out["error"]), (2, "page_unavailable"))

    def test_page_text_has_no_control_characters(self) -> None:
        code, _ = self.propose(content="Use \x1b[31mred\x1b[0m text.")
        self.assertEqual(code, 0)
        with self.assertRaises(json.JSONDecodeError):
            self.call("know", "context", "--page", "1")
        self.assertNotIn("\x1b", self.last_stdout)
        self.assertIn("[31mred", self.last_stdout)


class OrderTests(HookCase):
    """Put decisions and preferences first, then rules, facts and guides."""

    def test_old_decision_lands_on_page_1_and_order_is_stable(self) -> None:
        decision = self.add(
            kind="decision", content="Old decision.", created_at=1.0
        )
        constraint = self.add(
            kind="constraint", content="A constraint.", created_at=2.0
        )
        facts = [
            self.add(
                content=f"Fact {i}: " + "detail " * 60, created_at=20.0 + i
            )
            for i in range(12)
        ]
        preference = self.add(
            kind="preference", content="A preference.", created_at=5.0
        )
        start = self.body(self.start_hook())
        first = [m[2] for m in CODE.finditer(start)]
        self.assertEqual(first[:3], [preference, decision, constraint])
        context = knowledge_push.knowledge_context(
            self.conn, self.scope, None, time.time()
        )
        seen: list[str] = []
        for number in range(1, 20):
            try:
                page = hook_start.render_page(context, "repo", number)
            except ValueError:
                break
            seen += [m[2] for m in CODE.finditer(page)]
        self.assertEqual(
            seen,
            [preference, decision, constraint, *reversed(facts)],
        )


class PageLookupTests(HookCase):
    """Show only as many notes as page 1 needs."""

    def test_proof_lookups_stop_after_page_1(self) -> None:
        for i in range(12):
            self.add(
                content=f"Memory {i}: " + "detail " * 60, created_at=20.0 + i
            )
        with mock.patch(
            "muninn.hook.proof_view", wraps=hook.proof_view
        ) as shown:
            text = self.body(self.start_hook())
        packed = len(CODE.findall(text))
        self.assertTrue(0 < packed < 12)
        self.assertEqual(shown.call_count, packed + 1)


class PageCliManyTests(CliLearningCase):
    """Print several pages through the CLI for each provider."""

    def setUp(self) -> None:
        super().setUp()
        notes = [
            self.note(content=f"Fact {i}: " + "detail " * 60)
            for i in range(10)
        ]
        # The oldest fact, so it lands on a later page.
        notes[0] = self.note(content="Use \x1b[31mred\x1b[0m " * 3)
        notes.append(self.note(kind="decision", content="Model decision."))
        request = self.row(
            "SELECT * FROM review_request WHERE id=?",
            (self.context.request_id,),
        )
        # Stored through the checked writer, as the commit would.
        for at, change in enumerate(self.result(notes).changes):
            knowledge_write.apply_change(self.conn, change, request, 13.0 + at)

    def page(self, number: int, *flag: str) -> str:
        """Return one printed page."""
        with self.assertRaises(json.JSONDecodeError):
            self.call("know", "context", "--page", str(number), *flag)
        return self.last_stdout

    def test_pages_per_provider_and_refusals(self) -> None:
        for provider, flag in (
            ("codex", " --provider codex"),
            ("cursor", ""),
        ):
            with self.subTest(provider=provider):
                first = self.page(1, "--provider", provider)
                self.assertTrue(first.startswith(OPEN))
                # The decision comes first and shows its owner proof.
                self.assertIn("1. [owner] Use Python for this project.", first)
                self.assertNotIn("Model decision.", first)
                self.assertIn(f"--page 2{flag}\n", first)
                second = self.page(2, "--provider", provider)
                self.assertTrue(second.startswith(OPEN))
                self.assertIn("· page 2", second)
                pages = [first, second]
                while "more memories" in pages[-1]:
                    pages.append(
                        self.page(len(pages) + 1, "--provider", provider)
                    )
                (red,) = [
                    n for n, text in enumerate(pages, 1) if "[31mred" in text
                ]
                self.assertGreaterEqual(red, 2)
                self.assertNotIn("\x1b", "".join(pages))
        for argv, error in (
            (("--page", "0"), "page_unavailable"),
            (("--page", "1", "--task", "t"), "page_task_unsupported"),
            (("--provider", "codex"), "provider_needs_page"),
        ):
            with self.subTest(argv=argv):
                code, out = self.call("know", "context", *argv)
                self.assertEqual((code, out["error"]), (2, error))
