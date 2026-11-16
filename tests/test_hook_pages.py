"""Check the session-start list layout and its continuation pages."""

from __future__ import annotations

import json
import re
import time
import unittest

from muninn import hook, hook_frame, knowledge_push
from muninn.hook_banner import Item, banner
from muninn.knowledge_models import KnowledgeContext
from tests.hook_support import HookCase
from tests.test_cli_learning import CliLearningCase

CODE = re.compile(r"^▓ (\d+): (K_[0-9a-f]{32})$", re.M)
MORE = re.compile(
    r"^▓ (\d+) more memories: muninn know context --page (\d+)", re.M
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
                    page = hook_frame.render_page(
                        context, "repo", number, limit=limit, flag=flag
                    )
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
                    hook_frame.render_page(
                        context, "repo", number + 1, limit=limit
                    )

    def test_codex_pages_are_smaller(self) -> None:
        context = self.everything()
        sizes = [
            len(
                hook_frame.start_pages(context, "repo", limit, "")[0],
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
        self.assertTrue(page.startswith("▓" * 20))
        self.assertIn("· page 1 of 1 · 1 of 1", page)
        self.assertIn(f"▓ 1: {ident}", page)
        self.assertNotIn("more memories", page)
        code, out = self.call("know", "context", "--page", "2")
        self.assertEqual((code, out["error"]), (2, "page_unavailable"))
