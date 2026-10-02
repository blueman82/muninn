"""Prompt-time recall: pick what to show for a submitted prompt.

The search is deliberately strict. An event is recalled only if it matches
several distinct terms of the prompt, so a hook adds context when it is
clearly relevant and stays silent otherwise.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict, cast

from pctx import knowledge, query
from pctx.hook_frame import RecallEntry, RecallHit, recall_text

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
    # Extra lines, such as a stale-index warning. Called only once something
    # is recalled, so a prompt with no hit never reads the heartbeat file.
    notes: Callable[[], tuple[str, ...]]
    limit: int  # maximum characters of the block


class SearchPage(TypedDict):
    """The fields of a ``query.search`` result that recall reads."""

    knowledge: list[RecallEntry]
    hits: list[RecallHit]
    has_more: bool
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
    payload: Mapping[str, object], trace: dict[str, object]
) -> list[str] | None:
    """Extract the query terms of the prompt.

    Args:
        payload: The provider's hook payload.
        trace: Receives ``skipped`` and ``n_terms`` for the stage log.

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
    if len(terms) < MIN_TERMS:
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

    Those with a live user-prompt citation, as at SessionStart; the others
    stay pull-only.

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
    )


def _pool(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    cwd: str,
    terms: list[str],
) -> tuple[list[RecallEntry], list[RecallHit], int]:
    """Page through search results for events that pass the term floor.

    Returns:
        The pushable knowledge, events matching at least ``MIN_TERMS``
        terms (possibly more than ``MAX_EVENTS``), and how many events the
        floor dropped.
    """
    entries: list[RecallEntry] = []
    hits: list[RecallHit] = []
    dropped = 0
    for page in range(1, POOL_PAGES + 1):
        found = _search_page(conn, payload, env, cwd, page)
        if "error" in found:
            break
        if page == 1:  # knowledge does not change between pages
            entries = _pushable(conn, found["knowledge"])
        counts = _matched(conn, terms, [h["id"] for h in found["hits"]])
        for hit in found["hits"]:
            if counts[hit["id"]] >= MIN_TERMS:
                hits.append(hit)
            else:
                dropped += 1
        if len(hits) >= MAX_EVENTS or not found["has_more"]:
            break
    return entries, hits, dropped


def recall_block(
    conn: sqlite3.Connection,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    trace: dict[str, object],
    request: RecallRequest,
) -> str:
    """Build the recall block for the prompt.

    Args:
        conn: Read-only connection.
        payload: The provider's hook payload.
        env: Environment, for the caller's own session.
        trace: Receives ids and stage counts for the stage log.
        request: Terms, scope, notes and size limit.

    Returns:
        The framed block, or an empty string when nothing qualifies.
    """
    entries, hits, dropped = _pool(
        conn, payload, env, request.cwd, request.terms
    )
    hits = hits[:MAX_EVENTS]
    trace["returned_ids"] = [h["id"] for h in hits]
    trace["knowledge_ids"] = [int(e["id"][1:]) for e in entries]
    trace["stages"] = {"floor_dropped": dropped, "returned": len(hits)}
    if not entries and not hits:
        return ""
    return recall_text(entries, hits, request.notes(), request.limit)
