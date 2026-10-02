"""Read-only retrieval over the store: search, open, sessions, quotes.

Every function takes a connection from ``muninn.store`` (sqlite3.Row rows) and
only reads. Answers are plain JSON-serialisable dicts. Bad input comes back
as ``{"error": code}``; a store that cannot be read raises
``store.StoreUnavailableError`` (``store.HotJournalError`` when a crashed
writer left a journal), which callers map to exit 4.

The package is split by responsibility; this module is the one import path
other code uses.
"""

from __future__ import annotations

from muninn.query.constants import (
    ALL_KINDS,
    NOTICE,
    PREVIEW_NOTICE,
    PROVIDERS,
)
from muninn.query.filters import caller_root
from muninn.query.guard import guarded
from muninn.query.index_age import freshness
from muninn.query.listing import session, sessions
from muninn.query.opening import open_event
from muninn.query.quotes import quote_check
from muninn.query.ranked import search
from muninn.query.terms import STOPWORDS, build_fts_query, parse_ref

__all__ = [
    "ALL_KINDS",
    "NOTICE",
    "PREVIEW_NOTICE",
    "PROVIDERS",
    "STOPWORDS",
    "build_fts_query",
    "caller_root",
    "freshness",
    "guarded",
    "open_event",
    "parse_ref",
    "quote_check",
    "search",
    "session",
    "sessions",
]
