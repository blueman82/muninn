"""Lay out session-start memories as one numbered list between two bars."""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Sequence
from typing import NamedTuple

BAR = "▓"
MIN_WIDTH = 20
MAX_WIDTH = 72
WRAP = 70


class Item(NamedTuple):
    """One memory as it is shown: cleaned text and its code."""

    ident: str
    text: str
    conditions: tuple[str, ...]


def item_lines(number: int, item: Item) -> list[str]:
    """Wrap one numbered memory and its conditions."""
    lines = textwrap.wrap(
        item.text,
        WRAP,
        initial_indent=f"{number}. ",
        subsequent_indent="   ",
    ) or [f"{number}."]
    if item.conditions:
        lines += textwrap.wrap(
            "; ".join(item.conditions),
            WRAP,
            initial_indent="   When: ",
            subsequent_indent="   ",
        )
    return lines


def banner(head: str, items: Sequence[Item], footer: str | None) -> list[str]:
    """Return the lines of one page between its two bars.

    Args:
        head: Header text, already starting with the bar character.
        items: The memories on this page, in order.
        footer: The line that points to the next page, if any.

    Returns:
        The bar, header, numbered memories, one code line for each memory,
        the optional footer and the bar. The bar is as wide as the longest
        line, from 20 to 72 characters.
    """
    body = [
        line
        for number, item in enumerate(items, 1)
        for line in item_lines(number, item)
    ]
    codes = [
        f"{BAR} {number}: {item.ident}" for number, item in enumerate(items, 1)
    ]
    tail = [footer] if footer else []
    width = max(len(line) for line in (head, *body, *codes, *tail))
    bar = BAR * max(MIN_WIDTH, min(MAX_WIDTH, width))
    if not items:
        return [bar, head, *tail, bar]
    return [bar, head, "", *body, "", *codes, *tail, bar]


def more_line(count: int, page: int, flag: str) -> str:
    """Point to the next page of memories."""
    return (
        f"{BAR} {count} more memories. Before you write code, run: muninn"
        f" know context --page {page}{flag}"
    )


def paginate(
    count: int,
    get: Callable[[int], Item],
    fits: Callable[[list[Item], int, int], bool],
    too_long: Callable[[Item], Item],
    stop_after: int | None = None,
) -> list[list[Item]]:
    """Split memories into consecutive pages, each one once.

    Args:
        count: Number of eligible memories in their fixed order.
        get: The shown item at a position; called in order, only as far as
            the pages need, so its lookups can be lazy.
        fits: Whether a page with these items, this many left after it and
            this 1-based number fits its byte limit.
        too_long: The look-up item for a memory larger than a page.
        stop_after: Build at most this many pages, or all of them.

    Returns:
        The pages in order. A page takes items until the next one does not
        fit; the next page starts at that item. A memory that does not fit
        an empty page is shown alone, as its look-up item.
    """
    pages: list[list[Item]] = []
    at = 0
    while at < count and (stop_after is None or len(pages) < stop_after):
        page: list[Item] = []
        number = len(pages) + 1
        while at < count and fits([*page, get(at)], count - at - 1, number):
            page.append(get(at))
            at += 1
        if not page:
            page = [too_long(get(at))]
            at += 1
        pages.append(page)
    return pages
