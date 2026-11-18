"""Split a run of indexed JSONL lines into windows that fit the read limit.

A window is closed early when its original bytes would pass the safe limit.
A window that starts at such a cut repeats the last owner turn before the
cut as context, shortened (down to none) so that one full new turn still
fits. The turn rule is the only rule that sizes the overlap.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Literal

MAX_LINES = 200


@dataclass(frozen=True, slots=True)
class Line:
    """One eligible indexed line with its charged original bytes."""

    number: int
    size: int
    candidate: bool
    sha: str
    key: str


@dataclass(frozen=True, slots=True)
class Part:
    """One window as run indexes; `new` is the first new line."""

    first: int
    new: int
    last: int
    cut: bool


def safe_limit(max_input_bytes: int) -> int:
    """Return the charged-byte bound of one window under the policy limit.

    `_verify` charges a window about twice, and each pass may read up to
    8191 bytes more. Tiny limits fall back to a quarter of the limit.

    Args:
        max_input_bytes: The policy read limit.

    Returns:
        The largest byte count a window may charge.
    """
    return max((max_input_bytes - 2 * 8192) // 2, max_input_bytes // 4)


class Splitter:
    """Plan the windows of one run of same-scope eligible lines."""

    def __init__(
        self, lines: list[Line], prompts: list[int], end: int, safe: int
    ) -> None:
        """Keep the run, all prompt line numbers, the last line and bound."""
        self.lines = lines
        self.prompts = prompts
        self.promptset = set(prompts)
        self.end = end
        self.safe = safe
        self.numbers = [line.number for line in lines]
        self.sums = [0]
        self.cands = [0]
        for line in lines:
            self.sums.append(self.sums[-1] + line.size)
            self.cands.append(self.cands[-1] + int(line.candidate))

    def fits(self, first: int, last: int) -> bool:
        """Tell if the lines first..last pass the byte and line bounds."""
        return (
            self.numbers[last] - self.numbers[first] < MAX_LINES
            and self.sums[last + 1] - self.sums[first] <= self.safe
        )

    def turn_end(self, start: int) -> int:
        """Return the last physical line of the turn that starts there."""
        later = bisect_left(self.prompts, start + 1)
        if later < len(self.prompts):
            return self.prompts[later] - 1
        return self.end

    def overlap(self, new: int, floor: int) -> tuple[int, int | None]:
        """Pick the window start for a new part that starts at a cut.

        Args:
            new: Run index of the first new line.
            floor: Run index of the first line of the window before.

        Returns:
            The first line of the window and the index of the last line of
            the first full new turn, or None when there is no overlap.
        """
        number = self.numbers[new]
        after = bisect_left(self.prompts, number)
        if after == 0 or after == len(self.prompts):
            return new, None
        stop = self.turn_end(self.prompts[after])
        end = bisect_right(self.numbers, stop) - 1
        low = max(floor, bisect_left(self.numbers, self.prompts[after - 1]))
        for first in range(low, new):
            if self.fits(first, end):
                return first, end
        return new, None

    def extend(
        self, first: int, new: int
    ) -> tuple[int, Literal["cap", "cut", "end"]]:
        """Grow a window over following lines until a limit closes it."""
        last = new
        while last + 1 < len(self.lines):
            after = last + 1
            if self.numbers[after] - self.numbers[first] >= MAX_LINES:
                return last, "cap"
            if self.sums[after + 1] - self.sums[first] > self.safe:
                return last, "cut"
            last = after
        return last, "end"

    def trim(self, new: int, last: int, need: int | None) -> int:
        """Close before the last prompt that still leaves a candidate."""
        low = new if need is None else need
        for prompt in range(last + 1, new, -1):
            if (
                prompt - 1 >= low
                and self.numbers[prompt] in self.promptset
                and self.cands[prompt] > self.cands[new]
            ):
                return prompt - 1
        return last

    def parts(self) -> list[Part]:
        """Return every window of the run in order."""
        output: list[Part] = []
        new, floor, cut = 0, 0, False
        while new < len(self.lines):
            if self.lines[new].size > self.safe:
                output.append(Part(new, new, new, False))
                new, floor, cut = new + 1, new, False
                continue
            first, need = (new, None)
            if cut:
                first, need = self.overlap(new, floor)
            last, why = self.extend(first, new)
            more = last + 1
            hopeless = more < len(self.lines) and (
                self.lines[more].size > self.safe
            )
            if why == "cut" and not hopeless:
                last = self.trim(new, last, need)
            output.append(Part(first, new, last, cut))
            floor = first
            cut = why == "cut" and not hopeless
            new = last + 1
        return output
