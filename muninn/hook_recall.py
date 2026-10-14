"""Prompt-time recall: pick what to show for a submitted prompt.

The search is deliberately strict. An event is recalled only if it matches
several distinct terms of the prompt, so a hook adds context when it is
clearly relevant and stays silent otherwise.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict, cast

from muninn import knowledge, query
from muninn.hook_frame import RecallEntry, RecallHit, recall_text

# A prompt with fewer query terms recalls nothing; the search ORs terms, so
# the floor keeps one or two common words from recalling loose matches.
MIN_TERMS = 3
MAX_EVENTS = 3  # events in a recall block
EVENT_KINDS = frozenset({"prompt", "reply"})  # recalled at prompt time
# Search pages read to find MAX_EVENTS events that pass the term floor.
POOL_PAGE, POOL_PAGES = 5, 4


@dataclass(frozen=True)
class RecallRequest:
    """What ``recall_block`` needs beyond the payload and the store."""

    terms: list[str]  # the prompt's query terms, from prompt_terms
    cwd: str  # working directory that scopes the search
    # Extra lines, such as a stale-index or failed-sources warning. Called
    # only once something is recalled, so a prompt with no hit never reads
    # the heartbeat file.
    notes: Callable[[], tuple[str, ...]]
    limit: int  # maximum characters of the block


class SearchPage(TypedDict):
    """The fields of a ``query.search`` result that recall reads."""

    knowledge: list[RecallEntry]
    hits: list[RecallHit]
    has_more: bool
    stages: NotRequired[dict[str, int]]
    error: NotRequired[str]


class _Query(Protocol):
    """The ``query.search`` call recall makes, typed by its result."""

    search: Callable[..., SearchPage]


# query.search annotates its dict and status bare, which pyright strict
# reads as partly unknown; the protocol types this one call. Its result has
# the SearchPage keys (or an error key).
_QUERY = cast("_Query", query)


def _terms(fts: str | None) -> list[str]:
    """The quoted single-word terms of an FTS query, phrases left out."""
    return [e for e in (fts or "").split(" OR ") if e and " " not in e]


def prompt_terms(
    payload: Mapping[str, object],
    trace: dict[str, object],
    min_terms: int = MIN_TERMS,
) -> list[str] | None:
    """Extract the query terms of the prompt.

    Args:
        payload: The provider's hook payload.
        trace: Receives ``skipped`` and ``n_terms`` for the stage log.
        min_terms: The term floor; the default is ``MIN_TERMS``.

    Returns:
        The terms, or None when the prompt recalls nothing: no prompt, a
        slash command, or fewer than ``MIN_TERMS`` terms.
    """
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or prompt.lstrip().startswith("/"):
        trace["skipped"] = "prompt"
        return None
    terms = _terms(query.build_fts_query(prompt))
    trace["n_terms"] = len(terms)
    if len(terms) < min_terms:
        trace["skipped"] = "short"
        return None
    return terms


def _matched(
    conn: sqlite3.Connection, terms: list[str], ids: list[int]
) -> Counter[int]:
    """Count how many of the terms each event matches.

    Uses the FTS5 table itself, so the tokenizer is the one that ranked
    the hits.

    Returns:
        Matched-term counts keyed by event id.
    """
    counts: Counter[int] = Counter()
    marks = ",".join("?" * len(ids))
    for term in terms:
        rows = conn.execute(
            "SELECT rowid FROM event_fts WHERE event_fts MATCH ?"
            f" AND rowid IN ({marks})",
            (term, *ids),
        )
        counts.update(row[0] for row in rows)
    return counts


def _pushable(
    conn: sqlite3.Connection, entries: list[RecallEntry]
) -> list[RecallEntry]:
    """Keep the entries a hook may push without being asked.

    Those that may be pushed as at SessionStart (``knowledge.user_cited``):
    not expired, not restricted, and with a live user-prompt citation whose
    quote backs the text; the others stay pull-only.

    Returns:
        The entries with a live user-prompt citation, in input order.
    """
    ids = [int(e["id"][1:]) for e in entries]
    allowed = knowledge.user_cited(conn, ids)
    return [e for e, kid in zip(entries, ids, strict=True) if kid in allowed]


def _search_page(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    cwd: str,
    page: int,
) -> SearchPage:
    """Fetch one page of search results for the payload's prompt."""
    session = payload.get("session_id")
    return _QUERY.search(
        conn,
        str(payload["prompt"]),
        cwd=cwd,
        env=env,
        kinds=set(EVENT_KINDS),
        limit=POOL_PAGE,
        page=page,
        current_session=session if isinstance(session, str) else None,
        push_only=True,
    )


def _pool(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    cwd: str,
    terms: list[str],
    min_terms: int = MIN_TERMS,
) -> tuple[list[RecallEntry], list[RecallHit], int, dict[str, int], bool]:
    """Page through search results for events that pass the term floor.

    Returns:
        The pushable knowledge, events matching at least ``MIN_TERMS``
        terms (possibly more than ``MAX_EVENTS``), how many events the
        floor dropped, page 1's stage counts (empty when page 1 failed),
        and whether any page errored, leaving later events unread.
    """
    entries: list[RecallEntry] = []
    hits: list[RecallHit] = []
    dropped = 0
    stages: dict[str, int] = {}
    unread = False
    for page in range(1, POOL_PAGES + 1):
        found = _search_page(conn, payload, env, cwd, page)
        if "error" in found:
            unread = True  # the floor outcome is unknown
            break
        if page == 1:  # knowledge does not change between pages
            entries = _pushable(conn, found["knowledge"])
            stages = found.get("stages", {})  # whole-query, same per page
        counts = _matched(conn, terms, [h["id"] for h in found["hits"]])
        for hit in found["hits"]:
            if counts[hit["id"]] >= min_terms:
                hits.append(hit)
            else:
                dropped += 1
        if len(hits) >= MAX_EVENTS or not found["has_more"]:
            break
    return entries, hits, dropped, stages, unread


def _outcome(
    stages: dict[str, int], dropped: int, found: bool, unread: bool
) -> dict[str, int]:
    """Count-only outcome keys: page 1's tallies and at most one miss.

    Returns:
        ``matches`` and ``in_scope`` when page 1 answered, ``pool_error``
        when any page errored, plus the miss key (``miss_zero``,
        ``miss_outside`` or ``miss_floor``) when nothing was returned.
        ``miss_floor`` is left out when pages went unread.
    """
    out = {k: stages[k] for k in ("matches", "in_scope") if k in stages}
    if unread:
        out["pool_error"] = 1
    if found or "matches" not in out:
        return out
    if out["matches"] == 0:
        return out | {"miss_zero": 1}
    if out["in_scope"] == 0:
        return out | {"miss_outside": 1}
    return out | ({"miss_floor": 1} if dropped and not unread else {})


def recall_block(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    trace: dict[str, object],
    request: RecallRequest,
    min_terms: int = MIN_TERMS,
) -> str:
    """Build the recall block for the prompt.

    Args:
        conn: Read-only connection.
        payload: The provider's hook payload.
        env: Environment, for the caller's own session.
        trace: Receives ids and stage counts for the stage log.
        request: Terms, scope, notes and size limit.
        min_terms: The term floor; the default is ``MIN_TERMS``.

    Returns:
        The framed block, or an empty string when nothing qualifies.
    """
    entries, hits, dropped, page1, unread = _pool(
        conn, payload, env, request.cwd, request.terms, min_terms
    )
    hits = hits[:MAX_EVENTS]
    trace["returned_ids"] = [h["id"] for h in hits]
    trace["knowledge_ids"] = [int(e["id"][1:]) for e in entries]
    stages = {"floor_dropped": dropped, "returned": len(hits)}
    with contextlib.suppress(Exception):  # logging must not fail recall
        stages |= _outcome(page1, dropped, bool(entries or hits), unread)
    trace["stages"] = stages
    if not entries and not hits:
        return ""
    return recall_text(entries, hits, request.notes(), request.limit)
