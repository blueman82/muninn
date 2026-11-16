"""Pack session-start memories into pages, showing each one lazily."""

from __future__ import annotations

from collections.abc import Callable

from muninn.hook_banner import BAR, Item, banner, more_line, paginate
from muninn.hook_frame import (
    BLOCK_LIMIT,
    OPEN_HINT,
    SEARCH_HINT,
    USAGE,
    clean,
    frame,
)
from muninn.knowledge_models import (
    GuideIndex,
    KnowledgeContext,
    KnowledgeView,
    NoteBody,
)

type Show = Callable[[KnowledgeView], KnowledgeView]

# Owner decision 2026-11-16: agents read the first page, so the owner's
# decisions come first. Kinds not named here sort after facts.
_KIND_ORDER = {
    "decision": 0,
    "preference": 0,
    "constraint": 1,
    "procedure": 1,
    "fact": 2,
}


def _as_stored(view: KnowledgeView) -> KnowledgeView:
    """Show a view unchanged, for callers that already prepared it."""
    return view


def _ordered(context: KnowledgeContext) -> list[KnowledgeView | GuideIndex]:
    """Order notes by kind group, then the guide index.

    Args:
        context: Eligible notes and guides, each list newest first.

    Returns:
        Decisions and preferences, then constraints and procedures, then
        facts and other notes, then guides; newest first in each group,
        because the sort is stable.
    """
    notes = sorted(
        (view for view in context.notes if isinstance(view.body, NoteBody)),
        key=lambda view: _KIND_ORDER.get(
            view.body.kind if view.body else "", 3
        ),
    )
    return [*notes, *context.guides]


def _item(entry: KnowledgeView | GuideIndex, show: Show) -> Item:
    """Turn one note, shown for agents, or one guide title into an item."""
    if isinstance(entry, GuideIndex):
        return Item(entry.id, clean(entry.title, None), ())
    view = show(entry)
    body = view.body
    assert isinstance(body, NoteBody)
    return Item(
        view.id,
        clean(body.content, None),
        tuple(clean(c, None) for c in body.conditions),
    )


def _head(label: str, page: list[Item], total: int, number: str) -> str:
    """Return the header line of one page."""
    return (
        f"{BAR} MUNINN MEMORY · {clean(label, 80)} · {number}"
        f"{len(page)} of {total}"
    )


def start_pages(
    context: KnowledgeContext,
    label: str,
    limit: int,
    flag: str,
    *,
    show: Show = _as_stored,
    stop_after: int | None = None,
) -> list[list[Item]]:
    """Split eligible memories into session-start sized pages.

    Args:
        context: Every eligible note and guide, in reader order.
        label: Name of the mapped scope shown in the header.
        limit: The provider's session-start byte limit.
        flag: Extra ``know context`` option the footer must repeat.
        show: Prepares a note for agents; called only for notes the pages
            reach, because the shown text sets each item's size.
        stop_after: Build at most this many pages.

    Returns:
        Consecutive pages; page 1 is the session-start block.
    """
    entries = _ordered(context)
    shown: dict[int, Item] = {}

    def get(at: int) -> Item:
        if at not in shown:
            shown[at] = _item(entries[at], show)
        return shown[at]

    def fits(page: list[Item], left: int) -> bool:
        footer = more_line(left, 2, flag) if left else None
        lines = banner(_head(label, page, len(entries), ""), page, footer)
        block = frame("", [USAGE, *lines, SEARCH_HINT, OPEN_HINT])
        return len(block.encode()) <= limit

    def too_long(item: Item) -> Item:
        text = f"memory too long: run muninn know show {item.ident}"
        return Item(item.ident, text, ())

    return paginate(len(entries), get, fits, too_long, stop_after)


def render_block(
    context: KnowledgeContext,
    scope_label: str,
    *,
    notes: tuple[str, ...] = (),
    limit: int = BLOCK_LIMIT,
    flag: str = "",
    show: Show = _as_stored,
) -> str:
    """Render the first page of memories as the session-start block.

    Args:
        context: Every eligible note and guide index entry, in order.
        scope_label: Name of the mapped scope shown in the header.
        notes: Extra diagnostic lines, kept only when they fit.
        limit: Maximum UTF-8 bytes, including the complete frame.
        flag: Extra ``know context`` option the footer must repeat.
        show: Prepares a note for agents, for the notes page 1 reaches.

    Returns:
        Bounded untrusted evidence with whole memories only, or an empty
        string when even the empty frame does not fit.
    """
    total = len(context.notes) + len(context.guides)
    pages = start_pages(
        context, scope_label, limit, flag, show=show, stop_after=1
    )
    extra = [clean(note, 200) for note in notes]
    # Under a very small limit, keep the count and the pointer to the
    # memories before the optional help lines.
    for page in (pages[0] if pages else [], []):
        left = total - len(page)
        footer = more_line(left, 1 if not page else 2, flag) if left else None
        lines = [
            USAGE,
            *banner(_head(scope_label, page, total, ""), page, footer),
        ]
        for tail in (
            [SEARCH_HINT, OPEN_HINT, *extra],
            [SEARCH_HINT, OPEN_HINT],
            [SEARCH_HINT],
            [],
        ):
            block = frame("", [*lines, *tail])
            if len(block.encode()) <= limit:
                return block
    block = frame("", [USAGE])
    return block if len(block.encode()) <= limit else ""


def render_page(
    context: KnowledgeContext,
    scope_label: str,
    number: int,
    *,
    limit: int = BLOCK_LIMIT,
    flag: str = "",
    show: Show = _as_stored,
) -> str:
    """Render one page of memories for ``muninn know context --page``.

    Args:
        context: Every eligible note and guide index entry, in order.
        scope_label: Name of the mapped scope shown in the header.
        number: The 1-based page; page 1 matches the session-start block.
        limit: The provider's session-start byte limit that sets the pages.
        flag: Extra ``know context`` option the footer must repeat.
        show: Prepares a note for agents, for notes up to this page.

    Returns:
        The page inside the untrusted-data frame. Only pages up to this one
        are built, so the header says ``page N of N`` only on the last page.

    Raises:
        ValueError: If the page does not exist.
    """
    total = len(context.notes) + len(context.guides)
    pages = start_pages(
        context, scope_label, limit, flag, show=show, stop_after=number
    ) or [[]]
    if not 1 <= number <= len(pages):
        raise ValueError("page_unavailable")
    page = pages[number - 1]
    left = total - sum(len(p) for p in pages)
    last = f" of {number}" if not left else ""
    head = _head(scope_label, page, total, f"page {number}{last} · ")
    footer = more_line(left, number + 1, flag) if left else None
    # Later pages are the same untrusted data as the start block.
    return frame("", [USAGE, *banner(head, page, footer)])
