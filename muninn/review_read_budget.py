"""Account for original bytes, records and native VM work without refunds."""

from __future__ import annotations

from typing import Literal

from muninn.review_selectors import MAX_RECORDS, MAX_SCAN_BYTES

MAX_SQL_STEPS = 100_000
EVIDENCE_LIMIT_CODES = frozenset(
    {
        "evidence_limit",
        "evidence_byte_limit",
        "evidence_record_limit",
        "evidence_work_limit",
        "evidence_selection_limit",
    }
)


class EvidenceLimitError(ValueError):
    """Preserve a finite allowance cause without changing reader errors."""

    def __init__(
        self,
        code: Literal[
            "evidence_byte_limit",
            "evidence_record_limit",
            "evidence_work_limit",
            "evidence_selection_limit",
        ],
    ) -> None:
        """Attach only the program-owned exhausted allowance code."""
        super().__init__("evidence_limit")
        self.code = code


class ReadBudget:
    """Keep one operation's read grants separate from immutable policy."""

    def __init__(self, max_bytes: int) -> None:
        """Allocate positive bounded byte, record and native work grants."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("evidence_limit")
        self._grants = (
            min(max_bytes, MAX_SCAN_BYTES),
            MAX_RECORDS,
            MAX_SQL_STEPS,
        )
        self._used = [0, 0, 0]
        self._active = True
        self._partitionable = True

    @property
    def bytes_left(self) -> int:
        """The unspent byte grant still owned by this account."""
        return self._grants[0] - self._used[0] if self._active else 0

    @property
    def records_left(self) -> int:
        """The unspent record grant still owned by this account."""
        return self._grants[1] - self._used[1] if self._active else 0

    @property
    def sql_steps_left(self) -> int:
        """The unspent native callback grant owned by this account."""
        return self._grants[2] - self._used[2] if self._active else 0

    def consume_bytes(self, count: int) -> None:
        """Charge a nonnegative byte count without permitting overdraw."""
        if type(count) is not int or count < 0 or not self._active:
            raise ValueError("evidence_limit")
        if count > self.bytes_left:
            raise EvidenceLimitError("evidence_byte_limit")
        self._used[0] += count

    def consume_record(self) -> None:
        """Charge before processing one record, including repeated access."""
        if self.records_left == 0:
            raise EvidenceLimitError("evidence_record_limit")
        self._used[1] += 1

    def sqlite_progress(self) -> int:
        """Charge one native VM callback and request abort at exhaustion."""
        if self.sql_steps_left:
            self._used[2] += 1
        return int(self.sql_steps_left == 0)

    def partition(self, count: int) -> tuple[ReadBudget, ...]:
        """Transfer remaining grants once into positive isolated shares."""
        remaining = (self.bytes_left, self.records_left, self.sql_steps_left)
        if (
            type(count) is not int
            or count <= 0
            or not self._partitionable
            or min(remaining) < count
        ):
            raise ValueError("evidence_limit")
        divisions = [divmod(total, count) for total in remaining]
        children: list[ReadBudget] = []
        for index in range(count):
            grants = tuple(q + int(index < r) for q, r in divisions)
            child = ReadBudget(grants[0])
            child._grants = (grants[0], grants[1], grants[2])
            child._partitionable = False
            children.append(child)
        # Transfer removes funding authority without fabricating consumption.
        self._active = False
        self._partitionable = False
        return tuple(children)
