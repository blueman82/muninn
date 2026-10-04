"""muninn erase: remove sessions, event lines or matching text for good.

``erase`` needs the caller to hold ``store.writer_lock``; ``run_erase``
takes it. Targets are collected first. One transaction then inserts the
tombstones, deletes the events (their FTS rows go under secure-delete),
scrubs knowledge text and citation quotes, and deletes the source rows of
erased sessions. Tombstones and logs hold ids and hashes only; residue
needles and rare terms stay in memory. Provider transcripts are never
touched, only listed.
"""

from __future__ import annotations

import os
import shlex
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

from muninn import ingest, store
from muninn.erase_collect import (
    Target,
    collect_event,
    collect_knowledge,
    collect_match,
    collect_session,
)
from muninn.erase_residue import (
    aside_files,
    pick_needles,
    rare_terms,
    residue_scan,
    still_stored,
    vocab_left,
)
from muninn.tombstones import (
    TOMBSTONE_FILE,
    append_tombstones,
    reapply_tombstones,
)

__all__ = [
    "MIN_MATCH",
    "NOT_COVERED",
    "TOMBSTONE_FILE",
    "erase",
    "reapply_tombstones",
    "residue_scan",
    "run_erase",
]

MIN_MATCH = 4  # a 1-3 character match would erase nearly everything
NOT_COVERED = (
    "provider transcripts: muninn never deletes them (see provider_files)",
    "Time Machine and other backups",
    "free filesystem blocks (FileVault encrypts them at rest)",
)
# Copies of session content made by other tools; listed, never deleted.
_DERIVED = (
    ".codex/plugins/cache/muninn-local",
    ".codex/memories",
)


def _pick_mode(
    session: str | None, event_ref: str | None, match: str | None
) -> str:
    """Return which selector was given, or raise if not exactly one."""
    picked = [
        mode
        for mode, value in (
            ("session", session),
            ("event", event_ref),
            ("match", match),
        )
        if value is not None
    ]
    if len(picked) != 1:
        raise ValueError("give exactly one of session, event_ref, match")
    if match is not None and len(match.strip()) < MIN_MATCH:
        raise ValueError(f"match needs at least {MIN_MATCH} characters")
    return picked[0]


def _provider_files(target: Target, roots: Mapping[str, Path]) -> list[str]:
    """List transcript files the erased content came from (never touched)."""
    return sorted(
        {
            str(roots[s["root"]] / s["path"])
            for s in target.touched.values()
            if s["root"] in roots
        }
    )


def _out_of_scope(env: Mapping[str, str]) -> list[str]:
    """List other derived copies that exist, for the owner to deal with."""
    home = Path(env.get("HOME") or Path.home())
    found = [str(home / rel) for rel in _DERIVED if (home / rel).exists()]
    return sorted(found)


def _aside_report(home: Path) -> dict[str, object]:
    """Name each set-aside store erase cannot scrub, with the removal."""
    aside = aside_files(home)
    return {
        "aside_files": aside,
        "aside_remove": (
            "rm -- " + " ".join(shlex.quote(p) for p in aside)
            if aside
            else None
        ),
    }


def _apply(conn: sqlite3.Connection, target: Target) -> None:
    """Write the plan; the caller wraps this in one transaction."""
    conn.executemany(
        "INSERT INTO tombstone(created_at, provider, level, session_root,"
        " thread_id, line, line_sha256) VALUES (:created_at, :provider,"
        " :level, :session_root, :thread_id, :line, :line_sha256)",
        target.tombstones,
    )
    conn.executemany(  # the event_ad trigger removes the FTS rows
        "DELETE FROM event WHERE id = ?", [(i,) for i in target.events]
    )
    conn.executemany(
        "UPDATE citation SET quote = NULL, span_start = NULL,"
        " span_end = NULL, state = 'erased' WHERE id = ?",
        [(i,) for i in sorted(target.citations)],
    )
    now = time.time()
    for kid in sorted(target.knowledge):
        conn.execute(
            "UPDATE knowledge SET text = NULL, retract_reason = NULL,"
            " status = 'erased' WHERE id = ?",
            (kid,),
        )
        conn.execute(
            "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
            " VALUES (?, 'erase', 'user', ?)",
            (kid, now),
        )
    conn.executemany(  # source_issue and usage rows cascade
        "DELETE FROM source WHERE id = ?", [(i,) for i in target.sources]
    )


def _commit(conn: sqlite3.Connection, target: Target) -> None:
    """Apply the plan atomically, or leave the database untouched."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        _apply(conn, target)
        conn.execute("COMMIT")
    except BaseException:
        # BaseException: an interrupt mid-transaction must not leave the
        # connection holding the write lock.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _erase_and_verify(
    conn: sqlite3.Connection,
    home: Path,
    target: Target,
    match: str | None,
) -> dict[str, object]:
    """Commit the plan, then measure what survived."""
    needles = [match.encode()] if match else pick_needles(conn, target)
    # Both must be computed before the delete: afterwards the texts and the
    # vocabulary evidence are gone.
    rare = rare_terms(conn, target.events, sorted(target.knowledge))
    # Write-ahead: the tombstones reach disk before the commit, so a crash
    # in between cannot lose an erasure.
    append_tombstones(home, target.tombstones)
    _commit(conn, target)
    if not match:  # a needle still in a surviving row is not residue
        needles = [n for n in needles if not still_stored(conn, n)]
    return {
        "vocab": vocab_left(conn, rare),
        "needles": len(needles),
        # The list of leftover files, or 0 when the scan is clean.
        "residue": residue_scan(home, needles) or 0,
    }


def erase(
    conn: sqlite3.Connection,
    *,
    home: Path,
    session: str | None = None,
    event_ref: str | None = None,
    match: str | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] = os.environ,
) -> dict[str, object]:
    """Erase one session (with its forks), one event line or matching text.

    Exactly one of ``session``, ``event_ref`` and ``match`` is given. The
    caller holds ``store.writer_lock``. A ``ValueError`` propagates when
    not exactly one selector is given, ``match`` is shorter than
    ``MIN_MATCH`` characters or ``event_ref`` is malformed; a
    ``LookupError`` when ``event_ref`` matches no thread, several threads
    or no event. These are raised by callees, so they are described here
    rather than in a Raises section.

    Args:
        conn: Read-write connection.
        home: Data directory.
        session: Session root id to erase.
        event_ref: ``provider:thread:line.part`` of one event.
        match: Literal text; every text containing it is erased.
        dry_run: Report the counts without changing anything.
        env: Environment, for provider roots and ``HOME``.

    Returns:
        Counts and paths only; never erased text or the match string.
    """
    mode = _pick_mode(session, event_ref, match)
    target = Target()
    if session is not None:
        collect_session(conn, target, session)
    elif event_ref is not None:
        collect_event(conn, target, event_ref)
    elif match is not None:
        collect_match(conn, target, match)
    collect_knowledge(conn, target)
    out: dict[str, object] = {
        "dry_run": dry_run,
        "mode": mode,
        "sessions": len(target.sessions),
        "sources": len(target.sources),
        "lines": len(target.lines),
        "events": len(target.events),
        "citations": len(target.citations),
        "knowledge": len(target.knowledge),
        "tombstones": len(target.tombstones),
        "provider_files": _provider_files(target, ingest.default_roots(env)),
        "out_of_scope": _out_of_scope(env),
        "not_covered": list(NOT_COVERED),
    } | _aside_report(home)
    if dry_run:
        return out
    return out | _erase_and_verify(conn, home, target, match)


def run_erase(
    home: Path,
    *,
    wait_s: float = 15.0,
    session: str | None = None,
    event_ref: str | None = None,
    match: str | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] = os.environ,
) -> dict[str, object]:
    """Take the writer lock, open the store with full fsync, and erase.

    ``store.BusyError`` propagates if the writer lock is still held after
    ``wait_s``; ``erase`` raises ``ValueError`` and ``LookupError`` as
    documented there. Callee exceptions are described here, not in a
    Raises section.

    Args:
        home: Data directory.
        wait_s: Seconds to wait for the writer lock.
        session: Session root id to erase.
        event_ref: ``provider:thread:line.part`` of one event.
        match: Literal text; every text containing it is erased.
        dry_run: Report the counts without changing anything.
        env: Environment, for provider roots and ``HOME``.

    Returns:
        The result of ``erase``.
    """
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            return erase(
                conn,
                home=home,
                session=session,
                event_ref=event_ref,
                match=match,
                dry_run=dry_run,
                env=env,
            )
        finally:
            conn.close()
