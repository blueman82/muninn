"""Check that a read limit cuts or skips a window and never stops learning.

Fixtures are made-up Codex rollouts under a 64 KiB read limit. A live
capture or a catch-up pass must admit each window that fits the safe limit
of `review_split`, cut or skip each part that does not fit with one
content-free log record, and never stop with a read-limit code.
"""

from __future__ import annotations

import json
import os
from typing import cast
from unittest import mock

from muninn import (
    hook_learn,
    review_catchup,
    review_progress,
    review_read_budget,
)
from muninn.review_read_budget import EVIDENCE_LIMIT_CODES, ReadBudget
from muninn.review_request_views import request_view
from muninn.review_split import MAX_LINES, safe_limit
from tests.classify_support import custom_call
from tests.ingest_support import TID, Record, line
from tests.policy_patch import use_policy
from tests.test_classify import codex_meta, reply, user_msg
from tests.test_review_requests import RequestCase

LIMIT = 65_536
SAFE = safe_limit(LIMIT)
MARK = "QZXREADLIMITMARK"


def head(size: int = 0) -> Record:
    """Build the line-1 session record, with optional large instructions.

    Args:
        size: Extra bytes of base instructions; 0 keeps the usual record.

    Returns:
        The record.
    """
    if not size:
        return codex_meta("user", TID)
    return codex_meta(
        "user", TID, base_instructions={"text": MARK + "z" * size}
    )


def tools(first: int, count: int, size: int) -> list[Record]:
    """Build `count` tool-call records of about `size` bytes each.

    Args:
        first: Ordinal of the first record.
        count: Number of records.
        size: Bytes of tool input in each record.

    Returns:
        The records.
    """
    return [
        custom_call(first + i, "run", MARK + "x" * size, f"call-{i}")
        for i in range(count)
    ]


def long_chat() -> list[Record]:
    """Build 20 small turns whose sum passes the read limit.

    Returns:
        The records, with the session record first.
    """
    records = [head()]
    for index in range(20):
        ordinal = 1 + 3 * index
        records.append(user_msg(ordinal, f"q {index}"))
        records.extend(tools(ordinal + 1, 1, 4000))
        records.append(reply(ordinal + 2, f"a {index}"))
    return records


def tool_run_chat() -> list[Record]:
    """Build a turn whose tool lines before the reply pass the safe limit.

    Returns:
        Lines 1 to 13: session, prompt, 8 tool lines, reply, prompt, reply.
    """
    return [
        head(),
        user_msg(1, "q one"),
        *tools(2, 8, 5000),
        reply(10, "a one"),
        user_msg(11, "q two"),
        reply(12, "a two"),
    ]


def oversize_reply_chat(size: int) -> list[Record]:
    """Build a chat whose last line is one reply above the safe limit.

    Args:
        size: Bytes of the large reply text.

    Returns:
        Lines 1 to 5; line 5 is the large reply.
    """
    return [
        head(),
        user_msg(1, "q one"),
        reply(2, "a one"),
        user_msg(3, "q two"),
        reply(4, MARK + "y" * size),
    ]


class LimitCase(RequestCase):
    """Index one disposable Codex chat under an explicit read limit."""

    def setUp(self) -> None:
        """Install the read limit and the isolated native environment."""
        super().setUp()
        self.env = dict(os.environ) | {
            "HOME": str(self.tmp / "native-home"),
            "MUNINN_HOME": str(self.home),
        }
        self.policy = self.policy.model_copy(update={"max_input_bytes": LIMIT})
        use_policy(self, self.policy)

    def load(self, records: list[Record]) -> None:
        """Replace the chat file with records and index it again.

        Args:
            records: The records of the new chat file.
        """
        self.records = records
        self.path.write_bytes(b"".join(map(line, records)))
        self.bump(self.path)
        self.run_ingest(full=True)

    def capture(self) -> hook_learn.HookCapture:
        """Run one Codex SessionEnd capture of the chat.

        Returns:
            The capture result.
        """
        payload = {
            "session_id": TID,
            "cwd": str(self.tmp),
            "transcript_path": str(self.path),
            "hook_event_name": "SessionEnd",
            "reason": "other",
        }
        with mock.patch.dict(os.environ, self.env, clear=True):
            return hook_learn.capture_boundary(
                self.home,
                "codex",
                "session-end",
                json.dumps(payload).encode(),
                self.policy,
            )

    def step(self) -> None:
        """Run one catch-up pass in its own caller-owned transaction."""
        self.transaction(
            lambda: review_catchup.catch_up(self.conn, self.policy, 12.0, 0)
        )

    def settle(self) -> None:
        """Run enough catch-up passes to reach every window of the chat."""
        for _ in range(3 * len(self.records) + 5):
            self.step()

    def windows(self) -> list[tuple[int, int]]:
        """List the admitted request ranges in order.

        Returns:
            Pairs of first and last line.
        """
        return [
            (row[0], row[1])
            for row in self.conn.execute(
                "SELECT from_line,through_line FROM review_request "
                "ORDER BY from_line,through_line"
            )
        ]

    def candidates(self) -> set[int]:
        """List the indexed prompt and reply lines.

        Returns:
            The line numbers.
        """
        wanted = ("prompt", "reply")
        return {n for n, _, kind, _ in self.events() if kind in wanted}

    def covered(self) -> list[int]:
        """List each candidate line once for each request that holds it.

        Returns:
            Line numbers in order, with repeats for overlaps.
        """
        return sorted(
            number
            for number in self.candidates()
            for first, last in self.windows()
            if first <= number <= last
        )

    def size(self, first: int, last: int) -> int:
        """Count the original bytes of lines first to last.

        Args:
            first: First line, from 1.
            last: Last line.

        Returns:
            The byte count.
        """
        rows = self.path.read_bytes().splitlines(keepends=True)
        return sum(len(row) for row in rows[first - 1 : last])

    def logs(self) -> list[dict[str, object]]:
        """Read the call log records.

        Returns:
            One mapping for each log line, or none if there is no log.
        """
        path = self.home / "calls.jsonl"
        if not path.exists():
            return []
        return [
            cast(dict[str, object], json.loads(raw))
            for raw in path.read_text().splitlines()
        ]

    def skipped(self) -> list[dict[str, object]]:
        """Return the skip records of the call log.

        Returns:
            The records with the event `review_skipped`.
        """
        return [r for r in self.logs() if r.get("event") == "review_skipped"]

    def assert_fits(self) -> None:
        """Require each request to fit and to pass two controlled reads."""
        for first, last in self.windows():
            self.assertLess(last - first, MAX_LINES, (first, last))
            self.assertLessEqual(self.size(first, last), SAFE, (first, last))
        rows = self.conn.execute("SELECT * FROM review_request").fetchall()
        for row in rows:
            view = request_view(row)
            budget = ReadBudget(self.policy.max_input_bytes)
            for _ in range(2):
                review_progress.check_window(
                    self.conn,
                    view.window,
                    view.scope,
                    self.policy,
                    budget=budget,
                )

    def assert_clean(self) -> None:
        """Require content-free skip records and no read-limit refusal."""
        path = self.home / "calls.jsonl"
        if path.exists():
            self.assertNotIn(MARK, path.read_text())
        for record in self.logs():
            if record.get("event") == "review_skipped":
                self.assertIn(record.get("error_code"), EVIDENCE_LIMIT_CODES)
                count = record.get("skipped_lines")
                self.assertIs(type(count), int, record)
                self.assertGreaterEqual(cast(int, count), 1)
                self.assertNotIn("recovery_gaps", record)
                self.assertNotIn("missed_acknowledgments", record)
            else:
                self.assertNotIn(
                    record.get("error_code"), EVIDENCE_LIMIT_CODES, record
                )


class CaptureReadLimitTests(LimitCase):
    """A live capture admits, cuts or skips each window of one chat."""

    def test_long_chat_admits_every_window_with_its_own_budget(self) -> None:
        """A sum above the limit does not stop a capture of small windows."""
        self.load(long_chat())
        self.assertGreater(self.path.stat().st_size, LIMIT)
        result = self.capture()
        self.assertTrue(result.accepted, result.code)
        self.assertEqual(result.code, "persisted")
        self.assertEqual(self.covered(), sorted(self.candidates()))
        self.assertEqual(self.skipped(), [])
        self.assert_fits()
        self.assert_clean()

    def test_large_tool_run_is_cut_before_its_reply(self) -> None:
        """Leading tool lines are cut so that the reply window fits."""
        self.load(tool_run_chat())
        self.assertGreater(self.size(3, 11), SAFE)
        result = self.capture()
        self.assertTrue(result.accepted, result.code)
        self.assertEqual(self.covered(), sorted(self.candidates()))
        self.assertGreaterEqual(len(self.skipped()), 1)
        self.assert_fits()
        self.assert_clean()

    def check_oversize_reply(self, size: int) -> None:
        """Skip a large last reply once, then learn the lines after it.

        Args:
            size: Bytes of the large reply text.
        """
        self.load(oversize_reply_chat(size))
        self.assertGreater(self.size(5, 5), SAFE)
        first = self.capture()
        self.assertNotIn(first.code, EVIDENCE_LIMIT_CODES)
        self.assertTrue(first.accepted, first.code)
        self.assertEqual(self.covered(), [2, 3, 4])
        self.assertEqual(len(self.skipped()), 1)
        examined = self.conn.execute(
            "SELECT examined_line FROM review_source"
        ).fetchone()[0]
        self.assertGreaterEqual(examined, 5)
        self.records += [user_msg(5, "q three"), reply(6, "a three")]
        self.append(self.path, self.records[-2:])
        self.run_ingest()
        second = self.capture()
        self.assertTrue(second.accepted, second.code)
        self.assertEqual(self.covered(), [2, 3, 4, 6, 7])
        self.assertEqual(len(self.skipped()), 1)
        self.assert_fits()
        self.assert_clean()

    def test_reply_above_the_safe_limit_is_skipped_once(self) -> None:
        """A reply between the safe limit and the limit is skipped once."""
        self.check_oversize_reply(30_000)

    def test_reply_above_the_policy_limit_is_skipped_once(self) -> None:
        """A reply above the whole policy limit is skipped once."""
        self.check_oversize_reply(70_000)

    def test_large_first_line_does_not_stop_the_chat(self) -> None:
        """A line-1 record above the limit does not stop the capture."""
        self.load([head(70_000), user_msg(1, "q one"), reply(2, "a one")])
        self.assertGreater(self.size(1, 1), LIMIT)
        result = self.capture()
        self.assertTrue(result.accepted, result.code)
        self.assertEqual(self.covered(), [2, 3])
        self.assertGreaterEqual(len(self.skipped()), 1)
        self.assert_fits()
        self.assert_clean()

    def test_unavoidable_limit_skips_only_its_window(self) -> None:
        """A limit that a cut cannot avoid skips only the window it hits."""
        with mock.patch.object(review_read_budget, "MAX_RECORDS", 1):
            result = self.capture()
        self.assertNotIn(result.code, EVIDENCE_LIMIT_CODES)
        self.assertTrue(result.accepted, result.code)
        covered = self.covered()
        self.assertIn(3, covered)
        if 2 not in covered:
            self.assertGreaterEqual(len(self.skipped()), 1)
        self.assert_fits()
        self.assert_clean()


class CatchupReadLimitTests(LimitCase):
    """Catch-up cuts or skips a window once and then goes on."""

    def test_cut_and_skip_then_no_repeat(self) -> None:
        """Catch-up cuts, skips, learns the rest and then stays quiet."""
        records = tool_run_chat()
        records[-1] = reply(12, MARK + "y" * 30_000)
        records += [user_msg(13, "q three"), reply(14, "a three")]
        self.load(records)
        self.assertGreater(self.size(13, 13), SAFE)
        self.settle()
        self.assertEqual(self.covered(), [2, 11, 12, 14, 15])
        self.assertGreaterEqual(len(self.skipped()), 2)
        examined = self.conn.execute(
            "SELECT max(examined_line,eligible_line) FROM review_source"
        ).fetchone()[0]
        self.assertEqual(examined, 15)
        logs, windows = self.logs(), self.windows()
        for _ in range(3):
            self.step()
        self.assertEqual(self.logs(), logs)
        self.assertEqual(self.windows(), windows)
        self.assert_fits()
        self.assert_clean()

    def test_large_first_line_does_not_stop_catch_up(self) -> None:
        """A line-1 record above the limit does not stop catch-up."""
        self.load([head(70_000), user_msg(1, "q one"), reply(2, "a one")])
        self.settle()
        self.assertEqual(self.covered(), [2, 3])
        self.assert_fits()
        self.assert_clean()

    def test_long_chat_is_learned_in_full(self) -> None:
        """Catch-up learns each window of a chat above the limit.

        This test guards the behaviour of today (spec, Guard tests). It
        passes before the change, and the change must keep it green.
        """
        self.load(long_chat())
        self.settle()
        self.assertEqual(self.covered(), sorted(self.candidates()))
        self.assertEqual(self.skipped(), [])
        self.assert_fits()
        self.assert_clean()

    def test_unavoidable_limit_moves_on_without_repeat(self) -> None:
        """An unavoidable limit is skipped once and catch-up goes on."""
        with mock.patch.object(review_read_budget, "MAX_RECORDS", 1):
            self.settle()
            logs, windows = self.logs(), self.windows()
            for _ in range(3):
                self.step()
            self.assertEqual(self.logs(), logs)
            self.assertEqual(self.windows(), windows)
        covered = self.covered()
        self.assertIn(3, covered)
        if 2 not in covered:
            self.assertGreaterEqual(len(self.skipped()), 1)
        self.assert_fits()
        self.assert_clean()
