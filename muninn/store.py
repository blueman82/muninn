"""SQLite store: schema v2, connections, the writer lock, private files.

The store is one rollback-journal SQLite file under the data home.  Every
writer takes the flock in `writer_lock` first, so there is exactly one
writer process at a time.  Readers open the file read-only and do not need
the writer lock, but in rollback-journal mode a reader's SHARED lock can
briefly delay a writer's commit; that is why the writer sets busy_timeout.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from muninn.obs_log import log_poller
from muninn.store_migrate import migrate_v1_to_v2
from muninn.store_schema import SCHEMA_SQL, SCHEMA_VERSION


class StoreUnavailableError(Exception):
    """The store is absent, unreadable or not a schema this code speaks."""


class HotJournalError(StoreUnavailableError):
    """A crashed writer left a journal that only a writer can roll back."""


class BusyError(Exception):
    """Another process holds the writer lock."""


def data_home(env: Mapping[str, str] = os.environ) -> Path:
    """Return $MUNINN_HOME, else ~/.local/share/muninn.

    Args:
        env: Environment to read; a parameter so tests need not patch it.

    Returns:
        The data home directory; it may not exist yet.
    """
    configured = env.get("MUNINN_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "muninn"


# A store that could not be read is kept beside the new one under this
# prefix plus a UTC timestamp (see ``muninn rebuild``).
UNREADABLE_PREFIX = "muninn.sqlite.unreadable-"


def db_path(home: Path) -> Path:
    """Return the database file inside a data home."""
    return home / "muninn.sqlite"


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` and any parents, then force mode 0700 on the leaf."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def write_json_atomic(path: Path, obj: object) -> None:
    """Write JSON to a 0600 temp file in the same dir, then rename it over.

    A value JSON cannot encode raises TypeError or ValueError before anything
    is written; OSError propagates from the temp-file operations.

    Args:
        path: Destination file.
        obj: JSON-serialisable value; keys are sorted for stable bytes.
    """
    payload = json.dumps(obj, sort_keys=True).encode("utf-8")  # may raise
    # The temp file lives beside the target: rename is only atomic within one
    # filesystem, so readers see the old file or the new one, never a mix.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:  # mkstemp files are 0600
            handle.write(payload)
        Path(tmp).replace(path)
    except BaseException:
        # BaseException so an interrupt does not leave a stray temp file
        # holding the payload. The cleanup must not fail: the interrupt may
        # land just after the rename, when the temp file is already gone, and
        # a failing cleanup would replace the interrupt, so a poller told to
        # stop would carry on.
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


# Applied to every writer connection before any statement of ours.
_WRITER_PRAGMAS = (
    "PRAGMA busy_timeout=5000",
    # Zero freed pages: an erased event or knowledge entry must not survive
    # as readable text in the free list.
    "PRAGMA secure_delete=ON",
    "PRAGMA foreign_keys=ON",  # off by default and per connection
    # FULL keeps a committed transaction durable across power loss.
    "PRAGMA synchronous=FULL",
    "PRAGMA cache_size=-262144",  # 256 MiB: big replaces must not spill
)
# Public name so doctor can check the same pragmas without a real store.
WRITER_PRAGMAS = _WRITER_PRAGMAS


def _ensure_dir(path: Path) -> None:
    """Create a missing data dir privately; never touch an existing one."""
    if not path.is_dir():
        ensure_private_dir(path)


def _migrate_logged(conn: sqlite3.Connection, home: Path) -> None:
    """Run the v1 to v2 migration and leave one poller.log line about it.

    The line says migrated, raced (another writer won) or failed, with the
    row count and duration; logging is best effort and never fails an open.
    """
    started = time.monotonic()
    event, rows, exc = "migrated", None, None
    try:
        rows = migrate_v1_to_v2(conn)
        if rows is None:
            event = "migrate_raced"
    except BaseException as err:
        event, exc = "migrate_failed", type(err).__name__
        raise
    finally:
        log_poller(
            home,
            {
                "event": event,
                "from_v": 1,
                "to_v": SCHEMA_VERSION,
                "rows": rows,
                "ms": round((time.monotonic() - started) * 1000, 1),
                "exc": exc,
            },
        )


def _init_schema(conn: sqlite3.Connection, home: Path) -> None:
    """Create the schema on a brand-new database.

    Raises:
        StoreUnavailableError: If the file holds another schema version.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 1:
        _migrate_logged(conn, home)
        return
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise StoreUnavailableError(
            f"schema v{version}, need v{SCHEMA_VERSION}"
        )
    # All-or-nothing: DDL and user_version commit together. On failure the
    # transaction stays open and connect_rw's close() rolls it back.
    conn.executescript(
        f"BEGIN IMMEDIATE;{SCHEMA_SQL}"
        f"PRAGMA user_version={SCHEMA_VERSION};COMMIT;"
    )


def connect_rw(path: Path, fullfsync: bool = True) -> sqlite3.Connection:
    """Open the store for writing, creating and migrating it.

    Autocommit connection (callers issue BEGIN IMMEDIATE); rows are
    sqlite3.Row.

    Args:
        path: Database file; created with mode 0600 if absent.
        fullfsync: False is only for a re-derivable pre-build, where
            durability is traded for speed.

    Returns:
        An open read-write connection with the writer pragmas applied.

    Raises:
        StoreUnavailableError: If the journal mode is not 'delete' or the
            file holds another schema version.
    """
    _ensure_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)  # born private
    os.fchmod(fd, 0o600)  # tightened if it already existed
    os.close(fd)
    # isolation_level=None: sqlite3's implicit transactions would fight the
    # explicit BEGIN IMMEDIATE that every caller issues to take the write
    # lock up front.
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        for pragma in _WRITER_PRAGMAS:
            conn.execute(pragma)
        conn.execute(f"PRAGMA fullfsync={'ON' if fullfsync else 'OFF'}")
        # Rollback-journal mode (SQLite's default) is required, not just
        # assumed: it needs no -wal/-shm sidecar files, so a read-only
        # connection can still open the store, and a crashed writer is
        # recoverable through heal_hot_journal.  Fail loudly if the file was
        # switched to another mode behind our back.
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode != "delete":
            raise StoreUnavailableError(
                f"journal_mode is {mode!r}, need 'delete'"
            )
        _init_schema(conn, path.parent)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def writer_lock(home: Path, wait_s: float = 15.0) -> Generator[None]:
    """Hold the flock on home/writer.lock for the ``with`` block.

    Not reentrant (a second open() in one process conflicts): take it once
    at command entry and never nest. Never fork while holding it, because
    the child would inherit the descriptor and keep the lock after the
    parent releases it.

    Args:
        home: Data home; the lock file lives beside the database.
        wait_s: Seconds to wait for the lock; 0 means do not wait.

    Yields:
        Nothing; the lock is held until the block exits.

    Raises:
        BusyError: If the lock is still held elsewhere after ``wait_s``.
        OSError: If the home directory cannot be created or the lock file
            cannot be opened.
    """
    _ensure_dir(home)
    fd = os.open(home / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:  # contention raises, it does not return
                if time.monotonic() >= deadline:
                    raise BusyError("writer lock is held elsewhere") from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)  # closing releases the flock, also on SIGKILL


def _uri(path: Path, mode: str) -> str:
    """Return a SQLite file: URI for ``path`` with ``?mode=`` appended."""
    return path.absolute().as_uri() + f"?mode={mode}"


def connect_ro(path: Path) -> sqlite3.Connection:
    """Open the store read-only.

    Args:
        path: Database file.

    Returns:
        A read-only connection (``query_only``) whose rows are sqlite3.Row.

    Raises:
        HotJournalError: If a crashed writer left a journal that this
            read-only connection cannot roll back (see heal_hot_journal).
        StoreUnavailableError: If the store cannot be read or holds another
            schema version.
    """
    conn = None
    try:
        conn = sqlite3.connect(
            _uri(path, "ro"), uri=True, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        code = getattr(exc, "sqlite_errorcode", None)
        if code == sqlite3.SQLITE_READONLY_ROLLBACK:
            raise HotJournalError(
                "hot journal: a writer must roll it back"
            ) from exc
        name = getattr(exc, "sqlite_errorname", type(exc).__name__)
        raise StoreUnavailableError(f"cannot read the store ({name})") from exc
    if version != SCHEMA_VERSION:
        conn.close()
        raise StoreUnavailableError(
            f"schema v{version}, need v{SCHEMA_VERSION}"
        )
    return conn


def heal_hot_journal(path: Path, home: Path) -> bool:
    """Roll back a crashed writer's journal.

    Takes the writer lock without waiting and opens read-write once (the
    first read rolls the journal back).

    Args:
        path: Database file.
        home: Data home that holds the writer lock.

    Returns:
        True once a writer opened the file; False when the lock is busy or
        this process may not write (sandboxed), so the caller reports
        hot_journal.
    """
    try:
        with writer_lock(home, wait_s=0):
            conn = sqlite3.connect(_uri(path, "rw"), uri=True)
            try:
                conn.execute("PRAGMA user_version").fetchone()
            finally:
                conn.close()
    except (BusyError, OSError, sqlite3.Error):
        return False
    return True
