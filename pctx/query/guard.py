"""Map sqlite failures between statements onto StoreUnavailableError."""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable

from pctx.store import HotJournalError, StoreUnavailableError

# sqlite result codes that mean "the store cannot be read now", not "bug".
_STORE_TROUBLE = {
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
    sqlite3.SQLITE_READONLY,
    sqlite3.SQLITE_IOERR,
    sqlite3.SQLITE_CORRUPT,
    sqlite3.SQLITE_CANTOPEN,
    sqlite3.SQLITE_NOTADB,
}


def guarded[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Turn a store that fails between statements into StoreUnavailableError.

    ``connect_ro`` checks only at open time; a store that is locked past
    busy_timeout, turns hot, is corrupt or vanishes later raises here
    instead of a bare sqlite3 error (a HotJournalError for a crashed writer's
    journal). Translated messages carry the error name only, never values, so
    no stored text leaks through them; an unmatched sqlite3.Error is
    re-raised unchanged.

    Args:
        func: A reader that talks to the store.

    Returns:
        ``func`` with the same signature and the translated failures.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if code == sqlite3.SQLITE_READONLY_ROLLBACK:
                raise HotJournalError(
                    "hot journal: a writer must roll it back"
                ) from exc
            # The low byte is the primary result code; the high bits are
            # the extended code, which would never match the set above.
            if code is not None and (code & 0xFF) in _STORE_TROUBLE:
                name = getattr(exc, "sqlite_errorname", "sqlite error")
                raise StoreUnavailableError(
                    f"cannot read the store ({name})"
                ) from exc
            raise

    return wrapper
