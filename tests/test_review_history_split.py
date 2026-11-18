"""Check that history windows split to fit the read limit.

Fixtures are made-up Codex rollouts (the `RequestCase` source is a Codex
thread). They check the mechanics only: a split, the 200-line cap, a single
oversize line and small sources. The cut rules (turn overlap and its size)
are checked on the owner's real sessions in `test_real_sessions_report`.
There is no cap parameter and no fixed overlap number.
"""

from __future__ import annotations

from muninn import review_history
from muninn.review_history import HistoryItem, HistoryScan
from tests.classify_support import custom_call
from tests.ingest_support import Record, line
from tests.test_classify import reply, user_msg
from tests.test_review_requests import RequestCase

Span = tuple[int, int]

LIMIT = 1_048_576
SAFE = (LIMIT - 2 * 8192) // 2


def turn(ordinal: int, lines: int, size: int) -> list[Record]:
    """Build one owner turn of `lines` records: prompt, tools, reply."""
    records: list[Record] = [user_msg(ordinal, "q")]
    for step in range(lines - 2):
        records.append(custom_call(ordinal + 1 + step, "run", "x" * size))
    records.append(reply(ordinal + lines - 1, "a"))
    return records


def session(head: Record, count: int, lines: int, size: int) -> list[Record]:
    """Build a session of equal turns after the header record."""
    records = [head]
    for index in range(count):
        records.extend(turn(1 + index * lines, lines, size))
    return records


class HistorySplitTests(RequestCase):
    """Scan disposable originals whose windows may exceed the read limit."""

    def load(self, records: list[Record], limit: int = LIMIT) -> HistoryScan:
        """Replace the source with records and scan under the limit."""
        self.path.write_bytes(b"".join(line(r) for r in records))
        self.bump(self.path)
        self.run_ingest(full=True)
        policy = self.policy.model_copy(update={"max_input_bytes": limit})
        self.scanned = policy
        return review_history.scan(self.conn, policy, self.scope.project_id)

    def spans(self, scan: HistoryScan) -> list[Span]:
        """List verified item intervals in order."""
        return [
            (i.window.from_line, i.window.through_line) for i in scan.items
        ]

    def kinds(self, *wanted: str) -> list[int]:
        """List indexed lines whose event kind is one of wanted."""
        return sorted({n for n, _, kind, _ in self.events() if kind in wanted})

    def size(self, first: int, last: int) -> int:
        """Count original bytes of physical lines first..last."""
        rows = self.path.read_bytes().splitlines(keepends=True)
        return sum(len(r) for r in rows[first - 1 : last])

    def admit(self, item: HistoryItem) -> None:
        """Enqueue one history item as configure does."""
        self.transaction(
            lambda: review_history.enqueue_review(
                self.conn, item.window, item.scope, self.scanned,
                "history", 11.0,
            )
        )

    def test_large_lines_split_into_windows_that_all_verify(self) -> None:
        # 476 lines hold about 1.4 MB: one 200-line range cannot be read.
        scan = self.load(session(self.records[0], 14, 34, 3000))
        spans = self.spans(scan)
        self.assertEqual(scan.excluded, [])
        self.assertGreater(len(spans), 2)
        for item in scan.items:
            self.assertIsNotNone(item.content_digest)
            self.assertEqual(item.window.source.provider, "codex")
        for first, last in spans:
            self.assertLessEqual(last - first, 199)
            self.assertLessEqual(self.size(first, last), SAFE)

    def test_every_candidate_is_new_in_exactly_one_window(self) -> None:
        spans = self.spans(self.load(session(self.records[0], 14, 34, 3000)))
        parts: list[Span] = []
        for index, (first, last) in enumerate(spans):
            before = spans[index - 1][1] if index else 0
            parts.append((max(first, before + 1), last))
        for number in self.kinds("prompt", "reply"):
            hits = [p for p in parts if p[0] <= number <= p[1]]
            self.assertEqual(len(hits), 1, number)

    def test_single_oversize_line_stays_excluded_without_overlap(self) -> None:
        records = [
            self.records[0],
            user_msg(1, "q one"),
            reply(2, "a one"),
            reply(3, "y" * 150_000),
            user_msg(4, "q two"),
            reply(5, "a two"),
        ]
        scan = self.load(records, 100_000)
        self.assertEqual(self.spans(scan), [(2, 3), (5, 6)])
        self.assertEqual(
            [
                (e["from_line"], e["through_line"], e["code"])
                for e in scan.excluded
            ],
            [(4, 4, "evidence_limit")],
        )

    def test_admitted_windows_leave_nothing_to_learn_twice(self) -> None:
        scan = self.load(session(self.records[0], 14, 34, 3000))
        for item in scan.items:
            self.admit(item)
        again = review_history.scan(
            self.conn, self.scanned, self.scope.project_id
        )
        self.assertEqual(again.items, [])
        self.assertEqual(again.excluded, [])

    def test_small_source_is_unchanged(self) -> None:
        scan = review_history.scan(
            self.conn, self.policy, self.scope.project_id
        )
        self.assertEqual(self.spans(scan), [(2, 3)])
        self.assertEqual(scan.excluded, [])

    def test_two_hundred_line_cap_gives_no_overlap(self) -> None:
        records: list[Record] = [self.records[0]]
        for ordinal in range(1, 451, 2):
            records.append(user_msg(ordinal, "q"))
            records.append(reply(ordinal + 1, "a"))
        scan = self.load(records)
        self.assertEqual(
            self.spans(scan), [(2, 201), (202, 401), (402, 451)]
        )
        self.assertEqual(scan.excluded, [])

    def test_no_selected_cap_constant_exists(self) -> None:
        self.assertFalse(hasattr(review_history, "SELECTED_OVERLAP_CAP"))
