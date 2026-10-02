"""Split rendered hits into pages that fit the output size budget."""

from __future__ import annotations

import json
from typing import Any

from pctx import classify
from pctx.query.answers import Answer, error
from pctx.query.freshness import MAX_INDEX_AGE

OUTPUT_LIMIT = 6144  # bytes of json.dumps


def output_size(out: Answer) -> int:
    """Return the bytes the answer will occupy on the wire.

    Args:
        out: A search answer with ``hits`` and ``knowledge``.

    Returns:
        The size of the JSON line the CLI will print, including the fields
        it adds and any growth from redacting snippets and knowledge text.
    """
    # Reserve the largest freshness fields so a heartbeat tick cannot move
    # a hit across a page boundary. The CLI adds `logged` and a newline,
    # then redacts the text again, so both are counted here.
    wire = out | {
        "index_age_s": MAX_INDEX_AGE,
        "poller": "stale",
        "logged": False,
    }
    size = len(json.dumps(wire).encode("utf-8")) + 1
    for key, field in (("hits", "snippet"), ("knowledge", "text")):
        for item in out[key]:
            text = item[field]
            plain = (
                text.replace("«", "").replace("»", "")
                if key == "hits"
                else text
            )
            clean, changed = classify.redact(plain)
            if changed:
                size += max(0, len(json.dumps(clean)) - len(json.dumps(text)))
    return size


def _too_big(current: Answer) -> bool:
    """Say whether the page being built is over the size budget."""
    return output_size(current) > OUTPUT_LIMIT


def _new_page(
    out: Answer, hits: list[Answer], number: int, offset: int
) -> Answer:
    """Return an empty page; knowledge rides on page one only."""
    return out | {
        "page": number,
        "hits": [],
        "knowledge": (
            [dict(k) for k in out["knowledge"]] if number == 1 else []
        ),
        "has_more": offset < len(hits),
        "stages": out["stages"] | {"returned": 0},
    }


def _shrink_knowledge(current: Answer) -> None:
    """Halve the longest knowledge text until the page fits or none is left.

    Knowledge navigation stays on page one; only its previews get shorter.
    """
    while _too_big(current) and any(k["text"] for k in current["knowledge"]):
        entry = max(current["knowledge"], key=lambda k: len(k["text"]))
        entry["text"] = entry["text"][: len(entry["text"]) // 2]
        entry["text_truncated"] = True


def _truncate_snippet(current: Answer, hit: dict[str, Any]) -> Answer | None:
    """Cut a lone hit's snippet to the longest prefix that still fits.

    Args:
        current: The page, already holding ``hit`` as its only entry.
        hit: The oversized hit, edited in place.

    Returns:
        A refusal when the hit's metadata alone is too big, else None.
    """
    snippet = hit["snippet"]
    hit["snippet_truncated"] = True
    hit["snippet"] = ""
    if _too_big(current):
        return error("output_too_large") | {
            "note": f"Hit {hit['id']} metadata exceeds 6 KiB; "
            f"use pctx open {hit['id']} to inspect the original.",
        }
    # Binary search: the size grows monotonically with the prefix length.
    low, high = 0, len(snippet)
    while low < high:
        middle = (low + high + 1) // 2
        hit["snippet"] = snippet[:middle]
        if _too_big(current):
            high = middle - 1
        else:
            low = middle
    hit["snippet"] = snippet[:low]
    return None


def _fill(
    current: Answer, hits: list[Answer], offset: int, limit: int
) -> tuple[int, Answer | None]:
    """Add hits to a page until it is full by count or by size.

    Args:
        current: The page, edited in place.
        hits: Every rendered hit.
        offset: Index of the next hit to place.
        limit: Most hits on a page.

    Returns:
        The index of the next unplaced hit, and a refusal if one hit alone
        cannot fit.
    """
    while offset < len(hits) and len(current["hits"]) < limit:
        hit = dict(hits[offset])
        current["hits"].append(hit)
        current["stages"]["returned"] = len(current["hits"])
        current["has_more"] = offset + 1 < len(hits)
        if _too_big(current):
            if len(current["hits"]) > 1 or current["knowledge"]:
                current["hits"].pop()
                current["stages"]["returned"] = len(current["hits"])
                current["has_more"] = True
                break
            failure = _truncate_snippet(current, hit)
            if failure is not None:
                return offset, failure
        offset += 1
    return offset, None


def paginate(hits: list[Answer], out: Answer, limit: int, page: int) -> Answer:
    """Partition rendered hits into pages and return the requested one.

    Pages are built in order because where page N starts depends on how
    many hits earlier pages could hold within OUTPUT_LIMIT.

    Args:
        hits: Every rendered hit in display order.
        out: The answer so far (knowledge, stages and metadata).
        limit: Most hits on a page.
        page: The 1-based page wanted; past the end it returns the last.

    Returns:
        The page, or an ``output_too_large`` refusal when metadata alone
        exceeds the budget.
    """
    offset, number = 0, 1
    while True:
        current = _new_page(out, hits, number, offset)
        _shrink_knowledge(current)
        if _too_big(current):
            if current["knowledge"] and page > 1:
                # Knowledge fills a whole page; event hits start on the next.
                number += 1
                continue
            return error("output_too_large") | {
                "note": "Search metadata exceeds 6 KiB; narrow query/scope. "
                "For oversized knowledge, use pctx know list/show; "
                "search --page 2 continues to event hits.",
                "has_more": bool(current["knowledge"] and hits),
            }
        offset, failure = _fill(current, hits, offset, limit)
        if failure is not None:
            return failure
        if number == page:
            return current
        number = page if offset == len(hits) else number + 1
