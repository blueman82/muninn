"""``muninn rebuild``: derive a fresh store from the transcripts.

The events are re-derivable from the provider transcripts, but the
knowledge ledger, scopes and tombstones are not, so they are copied from the
old file when it is still readable.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import sqlite3
from argparse import Namespace
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from muninn import cli_core, erase, ingest, store
from muninn.cli_core import Env, Record, Result

__all__ = ["REBUILD", "rebuild"]

REBUILD = "muninn.sqlite.rebuild"
# Tables copied before and after the re-ingest.  Scopes and tombstones go
# first because ingest consults tombstones to skip deleted threads.
_BEFORE = ("scope", "scope_path", "tombstone")
_AFTER = ("knowledge", "citation", "knowledge_log")


@dataclasses.dataclass(frozen=True)
class _Built:
    """What building the new file produced."""

    readable: bool
    copied: dict[str, int]
    reapplied: int
    stats: ingest.PassStats
    quick_check: str


def _full_sync(path: Path) -> None:
    """Flush ``path`` to the platter, not just to the drive's cache."""
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            # macOS fsync leaves data in the drive cache; F_FULLFSYNC does not.
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except (AttributeError, OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _attach_old(conn: sqlite3.Connection, db: Path) -> bool:
    """ATTACH the old store as ``old`` if it is a readable current file."""
    if not db.exists():
        return False
    try:
        conn.execute("ATTACH DATABASE ? AS old", (str(db),))
        version = conn.execute("PRAGMA old.user_version").fetchone()[0]
        # Touch a table: a corrupt file can attach and still fail here.
        conn.execute("SELECT count(*) FROM old.event").fetchone()
        if version == store.SCHEMA_VERSION:
            return True
    except sqlite3.DatabaseError:
        pass
    with contextlib.suppress(sqlite3.Error):
        conn.execute("DETACH DATABASE old")
    return False


def _copy_old(
    conn: sqlite3.Connection, tables: Sequence[str]
) -> dict[str, int]:
    """Copy whole tables from ``old`` with ids kept, in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    # Supersede chains reference rows that may be inserted later in the
    # same copy; check the foreign keys at COMMIT instead of per row.
    conn.execute("PRAGMA defer_foreign_keys = ON")
    done = {
        t: conn.execute(f"INSERT INTO {t} SELECT * FROM old.{t}").rowcount
        for t in tables
    }
    conn.execute("COMMIT")
    return done


def _insert(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> int:
    """Run an INSERT and return the new row's id."""
    return cast("int", conn.execute(sql, tuple(params)).lastrowid)


def _copy_events(
    conn: sqlite3.Connection,
    old_source: int,
    new_source: int,
    ecols: list[str],
) -> int:
    """Copy one source's events, remapping parent ids; return the count."""
    ids: dict[int, int] = {}
    rows = conn.execute(
        f"SELECT id, {', '.join(ecols)} FROM old.event"
        " WHERE source_id = ? ORDER BY id",
        (old_source,),
    ).fetchall()
    for event in rows:
        values = dict(zip(ecols, tuple(event)[1:], strict=True))
        # ORDER BY id guarantees a parent is copied before its children.
        values["parent_event_id"] = ids.get(values["parent_event_id"])
        ids[event[0]] = _insert(
            conn,
            f"INSERT INTO event(source_id, {', '.join(ecols)}) VALUES"
            f" (?, {', '.join('?' * len(ecols))})",
            (new_source, *values.values()),
        )
    return len(ids)


def _copy_missing(conn: sqlite3.Connection) -> dict[str, int]:
    """Keep the events of sources the provider has since deleted.

    The transcript is gone, so these events cannot be re-derived; each
    source is copied in its own transaction.

    Args:
        conn: Connection to the new store with the old one attached.

    Returns:
        Counts of ``missing_sources`` and ``missing_events`` copied.
    """
    # Skip the leading id column of each table: new ids are assigned.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(source)")][1:]
    ecols = [r[1] for r in conn.execute("PRAGMA table_info(event)")][2:]
    gone = conn.execute(
        f"SELECT id, {', '.join(cols)} FROM old.source o WHERE"
        " status = 'missing' AND NOT EXISTS (SELECT 1 FROM main.source m"
        " WHERE m.provider = o.provider AND m.thread_id = o.thread_id)"
    ).fetchall()
    copied = {"missing_sources": 0, "missing_events": 0}
    for row in gone:
        conn.execute("BEGIN IMMEDIATE")
        try:
            new_id = _insert(
                conn,
                f"INSERT INTO source({', '.join(cols)}) VALUES"
                f" ({', '.join('?' * len(cols))})",
                tuple(row)[1:],
            )
        except sqlite3.IntegrityError:  # its path now holds another thread
            conn.execute("ROLLBACK")
            continue
        n_events = _copy_events(conn, row[0], new_id, ecols)
        conn.execute(
            "INSERT INTO usage SELECT ?, provider, session_root, calls,"
            " errors, last_ts FROM old.usage WHERE source_id = ?",
            (new_id, row[0]),
        )
        conn.execute("COMMIT")
        copied["missing_sources"] += 1
        copied["missing_events"] += n_events
    return copied


def _settle_journal(home: Path, db: Path) -> None:
    """Roll back a leftover journal by opening the old file once."""
    if (home / "muninn.sqlite-journal").exists():
        settle = sqlite3.connect(db)
        try:
            settle.execute("SELECT count(*) FROM sqlite_master")
        finally:
            settle.close()


def _build(home: Path, db: Path, new: Path, env: Env) -> _Built:
    """Create ``new`` from the transcripts and the old file's ledger."""
    for stale in (new, home / f"{REBUILD}-journal"):
        if stale.exists():
            stale.unlink()
    # No full fsync while building: everything but the copied knowledge can
    # be re-derived, and the file is synced once before it replaces the old.
    conn = store.connect_rw(new, fullfsync=False)
    try:
        readable = _attach_old(conn, db)
        copied = _copy_old(conn, _BEFORE) if readable else {}
        # Tombstones go in before ingest so erased text is never re-read.
        reapplied = erase.reapply_tombstones(conn, home)
        stats = ingest.ingest(conn, ingest.default_roots(env), full=True)
        if readable:
            copied |= _copy_old(conn, _AFTER)
            copied |= _copy_missing(conn)
            conn.execute("DETACH DATABASE old")
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    return _Built(readable, copied, reapplied, stats, quick)


def rebuild(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Replace the store with one rebuilt from the transcripts.

    Scopes, knowledge, tombstones and the events of deleted sources are
    copied from the old file when it is readable; ``tombstones.jsonl`` is
    re-applied first.  The old file is replaced only after the new one
    passes ``quick_check`` and has no hot journal.  A writer lock that stays
    busy propagates ``store.BusyError``; the dispatcher maps it to exit 3.

    Args:
        a: Parsed arguments (unused).
        env: Process environment, for the provider roots.
        home: Data directory.
        record: Call-log entry; receives the copy counts.

    Returns:
        Exit 0 and a summary, 2 if the new file fails its check, or 4 if
        it left a hot journal.
    """
    db, new = store.db_path(home), home / REBUILD
    with store.writer_lock(home, wait_s=cli_core.WRITER_WAIT_S):
        _settle_journal(home, db)
        built = _build(home, db, new, env)
        if built.quick_check != "ok":
            new.unlink()
            return 2, {"error": "quick_check_failed"}
        if (home / "muninn.sqlite-journal").exists():
            new.unlink()
            return 4, {"error": "hot_journal"}
        _full_sync(new)  # the copied knowledge is not re-derivable
        new.replace(db)
        _full_sync(home)  # make the rename itself durable
    record["counts"] = built.copied | {"reapplied": built.reapplied}
    return 0, {
        "rebuilt": True,
        "old_readable": built.readable,
        "copied": built.copied,
        "reapplied_tombstones": built.reapplied,
        "ingest": dataclasses.asdict(built.stats),
    }
