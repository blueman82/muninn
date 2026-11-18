"""Check the cut rules on the owner's real Claude and Codex sessions.

The owner allowed this one test file to read the owner's session files in
`~/.claude/projects` and `~/.codex/sessions`, read only ("we dont need fake
data"). This overrides the made-up-data rule in CLAUDE.md for this file
only. No session text is copied into the repo, logs or output: only line
numbers and byte counts are handled. The sessions grow every day, so the
tests check rules, never numbers; they pass when a folder has no cut. Each
folder is skipped when it is missing (for example on GitHub).

The helper models only bytes and the 200-line cap. The cap is today's:
a window spans at most 200 file line numbers (`through - from + 1`),
not 200 indexed lines. Windows start and end on indexed lines, bytes are
charged from indexed offsets, and "one more line of overlap" is one more
indexed line. Windows
that `layout` marks `limited` (records, scope, global or covered lines)
are left out of rules (b) to (e). One synthetic test with a real cut runs
the same checks, so they cannot pass only because no cut exists.

Owner decision on rule (a): the charge runs up to the next indexed line
(the index stores no line end), so a single indexed line may be charged
over the safe limit while `_verify`, which reads only through the line,
accepts it. Such a verified single-line window is allowed; any verified
window of two or more indexed lines must fit.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.classify_support import custom_call
from tests.ingest_support import Record, line
from tests.test_classify import reply, user_msg
from tests.test_review_requests import RequestCase
from tools import measure_history_windows

CLAUDE = Path.home() / ".claude" / "projects"
CODEX = Path.home() / ".codex" / "sessions"
DEFAULT_LIMIT = 1_048_576
SMALL_LIMIT = 262_144
MAX_LINES = 200
MAX_FILE_BYTES = 40_000_000
TOTAL_BYTES = 80_000_000


def sample(folder: Path) -> list[Path]:
    """Return a bounded, newest-first set of files to ingest.

    Real data is gigabytes and grows daily; each file is ingested once
    per limit, so the work is bounded: files over MAX_FILE_BYTES are
    left out, and files are taken newest first until TOTAL_BYTES.
    """
    found: list[tuple[float, int, Path]] = []
    for path in folder.rglob("*.jsonl"):
        try:
            info = path.stat()
        except OSError:  # deleted or unreadable during the scan
            continue
        found.append((info.st_mtime, info.st_size, path))
    chosen: list[Path] = []
    total = 0
    for _, size, path in sorted(found, reverse=True):
        if size > MAX_FILE_BYTES or total + size > TOTAL_BYTES:
            continue
        chosen.append(path)
        total += size
    return chosen


def safe(limit: int) -> int:
    """Return the charged-bytes bound of one window under the limit."""
    return (limit - 2 * 8192) // 2


class Layout:
    """Answer size questions about one file from numbers only."""

    def __init__(self, data: dict[str, Any], limit: int) -> None:
        """Keep the layout numbers and the safe bound."""
        self.data = data
        self.bound = safe(limit)
        self.starts: list[int] = data["turn_starts"]
        self.sizes: list[int] = data["line_bytes"]
        self.indexed: list[int] = data["indexed"]

    def size(self, first: int, last: int) -> int:
        """Count charged bytes of indexed lines first..last.

        Bytes run from the offset of `first` to the next indexed line
        (or the end of the file), so unindexed lines in between count.
        """
        later = [n for n in self.indexed if n > last]
        end = (later[0] - 1) if later else int(self.data["lines"])
        return sum(self.sizes[first - 1 : end])

    def fits(self, first: int, last: int) -> bool:
        """Tell if the range is within the byte and line bounds."""
        over = self.span(first, last) > MAX_LINES
        return not over and self.size(first, last) <= self.bound

    def span(self, first: int, last: int) -> int:
        """Count the file line numbers first..last (the 200-line cap).

        The cap is today's: a window spans at most 200 file line
        numbers, not 200 indexed lines (review_history `_windows`
        closes when `line - first >= 200`).
        """
        return last - first + 1

    def through_size(self, first: int, last: int) -> int:
        """Count bytes of first..last, through line `last` only."""
        return sum(self.sizes[first - 1 : last])

    def first_over(self, first: int, later: list[int]) -> int | None:
        """Return the first later indexed line that no longer fits.

        The charge runs to the next indexed line (the gap after the
        line is charged), as the builder charges it. A close at this
        line is the accepted trailing-gap early end, so it never fails
        rule (g).
        """
        for n in later:
            too_big = self.size(first, n) > self.bound
            if too_big or self.span(first, n) > MAX_LINES:
                return n
        return None

    def previous_indexed(self, line_no: int) -> int | None:
        """Return the indexed line before the line, if any."""
        before = [n for n in self.indexed if n < line_no]
        return before[-1] if before else None

    def turn_end(self, start: int) -> int:
        """Return the last indexed line of the turn from start."""
        later = [s for s in self.starts if s > start]
        stop = (later[0] - 1) if later else int(self.data["lines"])
        held = [n for n in self.indexed if start <= n <= stop]
        return held[-1]

    def first_full_turn_end(self, first: int, last: int) -> int | None:
        """Return the end of the first full turn inside first..last."""
        for start in self.starts:
            if start >= first and self.turn_end(start) <= last:
                return self.turn_end(start)
        return None

    def last_start_before(self, line_no: int) -> int | None:
        """Return the last turn start before the line."""
        before = [s for s in self.starts if s < line_no]
        return before[-1] if before else None


class CutRules(unittest.TestCase):
    """Rule checks shared by the real and the synthetic tests."""

    def check_window_rules(self, view: Layout, name: str) -> int:
        """Assert rules (a), (b) and (f); return the cuts seen."""
        wins: list[dict[str, Any]] = view.data["windows"]
        for index, win in enumerate(wins):
            first, last = win["from"], win["through"]
            self.assertIn(first, view.indexed, name)
            self.assertIn(last, view.indexed, name)
            if win["excluded"]:
                if not win["limited"]:
                    self.assertEqual(first, last, name)
                    over = view.size(first, last) - view.bound
                    self.assertGreater(over, 0, name)
                continue
            # (a): a verified window fits, or is a single indexed line
            # that _verify accepted (charge to the next indexed line
            # may pass the bound; owner decision).
            if first != last:
                self.assertTrue(view.fits(first, last), name)
            self.assertLessEqual(first, win["new_from"], name)
            if index:
                self.assertGreater(last, wins[index - 1]["through"], name)
            if win["new_from"] > first:
                self.assertTrue(win["cut"], name)
                self.assertGreaterEqual(first, wins[index - 1]["from"], name)
        return sum(1 for win in wins if win["cut"] and not win["excluded"])

    def check_turn_rule(self, view: Layout, name: str) -> None:
        """Assert rules (c), (d) and (e) for every plain cut window."""
        wins: list[dict[str, Any]] = view.data["windows"]
        for index, win in enumerate(wins):
            if win["limited"] or win["excluded"]:
                continue
            if not win["cut"] or not index:
                continue
            first, new, last = win["from"], win["new_from"], win["through"]
            end = view.first_full_turn_end(new, last)
            if new > first:
                self.assertIsNotNone(end, name)  # (c)
            if end is None:
                self.assertEqual(first, new, name)
                continue
            floor = wins[index - 1]["from"]
            turn = view.last_start_before(new)
            if turn is None:
                continue  # no prompt before the cut: no overlap rule
            floor = max(floor, turn)
            more = view.previous_indexed(first)
            # (d), (e): one more INDEXED line must not fit
            if more is not None and more >= floor:
                self.assertFalse(view.fits(more, end), name)

    def check_minimal(self, view: Layout, name: str) -> None:
        """Assert rule (g): a plain window closes only when needed."""
        wins: list[dict[str, Any]] = view.data["windows"]
        for index, win in enumerate(wins[:-1]):
            follow = wins[index + 1]
            if win["limited"] or win["excluded"] or follow["excluded"]:
                continue
            after = [n for n in view.indexed if n > win["through"]]
            if not after or follow["new_from"] != after[0]:
                continue
            nxt = after[0]
            first = win["from"]
            stop = view.first_over(first, after)
            self.assertIsNotNone(stop, name)  # window ended with room left
            if stop is None or nxt == stop:
                continue
            by_cap = view.span(first, stop) > MAX_LINES
            by_cap = by_cap and view.size(first, stop) <= view.bound
            self.assertFalse(by_cap, name)  # the cap closes exactly at L
            self.assertIn(nxt, view.starts, name)  # only a prompt may
            later = [s for s in view.starts if nxt < s <= stop]
            self.assertEqual(later, [], name)  # the last prompt before L

    def check_file(self, path: Path, limit: int, name: str) -> int:
        """Measure one file read only; check its rules; count cuts."""
        data = measure_history_windows.layout(path, limit)
        view = Layout(data, limit)
        cuts = self.check_window_rules(view, name)
        self.check_turn_rule(view, name)
        self.check_minimal(view, name)
        return cuts


class MinimalCloseTests(CutRules):
    """Rule (g) must fail for windows that close without need."""

    def view(
        self,
        windows: list[dict[str, Any]],
        starts: list[int] | None = None,
    ) -> Layout:
        """Build a hand-made layout: 6 lines of 100 bytes, bound 350."""
        data: dict[str, Any] = {
            "lines": 6,
            "line_bytes": [100] * 6,
            "indexed": [1, 2, 3, 4, 5, 6],
            "turn_starts": starts or [1, 3, 5],
            "windows": windows,
        }
        view = Layout(data, DEFAULT_LIMIT)
        view.bound = 350
        return view

    def win(self, first: int, last: int) -> dict[str, Any]:
        """Return a plain verified window with no overlap."""
        return {
            "from": first,
            "through": last,
            "new_from": first,
            "cut": False,
            "excluded": False,
            "limited": False,
        }

    def test_closing_at_every_prompt_fails(self) -> None:
        # every line is a prompt: closing at 3 leaves prompt 4 before L
        every = [1, 2, 3, 4, 5, 6]
        view = self.view([self.win(1, 2), self.win(3, 6)], every)
        with self.assertRaises(AssertionError):
            self.check_minimal(view, "hand")

    def test_single_line_windows_fail(self) -> None:
        view = self.view([self.win(n, n) for n in range(1, 7)])
        with self.assertRaises(AssertionError):
            self.check_minimal(view, "hand")

    def test_close_before_last_prompt_passes(self) -> None:
        # 1..2 fits (200); L is line 4 (400 > 350); nxt 3 is a prompt
        # and no prompt lies in (3, 4]: allowed by the cut preference.
        view = self.view([self.win(1, 2), self.win(3, 6)])
        self.check_minimal(view, "hand")

    def test_non_prompt_close_before_limit_fails(self) -> None:
        # L is 4 (400 > 350); nxt 2 is not a prompt and 2 < 4: fails.
        view = self.view([self.win(1, 1), self.win(2, 6)])
        with self.assertRaises(AssertionError):
            self.check_minimal(view, "hand")


class RealSessionRuleTests(CutRules):
    """Read real sessions and check the rules of the cut."""

    def setUp(self) -> None:
        """Keep the store of the user out of reach."""
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = mock.patch.dict(os.environ, {"MUNINN_HOME": home.name})
        env.start()
        self.addCleanup(env.stop)

    def run_folder(self, name: str, folder: Path, limit: int) -> None:
        """Check the rules of each file; print counts only."""
        files = sample(folder)
        cuts = sum(self.check_file(path, limit, name) for path in files)
        print(
            f"history cut rules, {name}, limit {limit}: "
            + json.dumps({"files": len(files), "cuts": cuts}),
            file=sys.stderr,
        )

    def folder_rules(self, name: str, folder: Path) -> None:
        """Skip a missing folder; check both limits otherwise."""
        if not folder.is_dir() or not sample(folder):
            self.skipTest(f"no {name} session folder")
        for limit in (DEFAULT_LIMIT, SMALL_LIMIT):
            with self.subTest(limit=limit):
                self.run_folder(name, folder, limit)

    def test_cut_rules_on_claude_sessions(self) -> None:
        self.folder_rules("claude", CLAUDE)

    def test_cut_rules_on_codex_sessions(self) -> None:
        self.folder_rules("codex", CODEX)


class SyntheticCutRuleTests(CutRules, RequestCase):
    """Run the same rule checks once on made-up data that has a cut."""

    def test_rules_hold_on_a_synthetic_cut(self) -> None:
        records: list[Record] = [self.records[0]]
        for index in range(14):
            base = 1 + index * 34
            records.append(user_msg(base, "q"))
            records.extend(
                custom_call(base + 1 + n, "run", "x" * 3000) for n in range(32)
            )
            records.append(reply(base + 33, "a"))
        self.path.write_bytes(b"".join(line(r) for r in records))
        cuts = self.check_file(self.path, DEFAULT_LIMIT, "synthetic")
        self.assertGreater(cuts, 0)
