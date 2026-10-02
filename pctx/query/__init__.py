"""Read-only retrieval over the store: search, open, sessions, quotes.

Every function takes a connection from ``pctx.store`` (sqlite3.Row rows) and
only reads. Answers are plain JSON-serialisable dicts. Bad input comes back
as ``{"error": code}``; a store that cannot be read raises
``store.StoreUnavailableError`` (``store.HotJournalError`` when a crashed
writer left a journal), which callers map to exit 4.

The package is split by responsibility; this module is the one import path
other code uses.
"""

from __future__ import annotations

from pctx.query.constants import (
    ALL_KINDS,
    NOTICE,
    PREVIEW_NOTICE,
    PROVIDERS,
)
from pctx.query.filters import caller_root
from pctx.query.freshness import freshness as _freshness
from pctx.query.guard import guarded as _guarded
from pctx.query.listing import session, sessions
from pctx.query.opening import open_event
from pctx.query.quotes import quote_check
from pctx.query.ranked import search
from pctx.query.terms import STOPWORDS, build_fts_query, parse_ref

__all__ = [
    "ALL_KINDS",
    "NOTICE",
    "PREVIEW_NOTICE",
    "PROVIDERS",
    "STOPWORDS",
    "_freshness",
    "_guarded",
    "build_fts_query",
    "caller_root",
    "open_event",
    "parse_ref",
    "quote_check",
    "search",
    "session",
    "sessions",
]
