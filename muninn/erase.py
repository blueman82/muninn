"""muninn erase: remove sessions, event lines or matching text for good.

``erase`` needs caller-held writer ownership and established producer
quiescence for real erasure; ``run_erase`` establishes both. Targets are
collected first. One transaction then inserts the
tombstones, deletes the events (their FTS rows go under secure-delete),
scrubs revisioned knowledge and review content, and deletes source rows of
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
from contextlib import ExitStack, suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from muninn import ingest, query, store
from muninn.erase_collect import (
    Target,
    collect_event,
    collect_knowledge,
    collect_match,
    collect_session,
    content_tombstones,
)
from muninn.erase_residue import (
    aside_files,
    pick_needles,
    rare_terms,
    residue_scan,
    still_stored,
    vocab_left,
)
from muninn.knowledge_erase import (
    ErasureSummary,
    erase_attempt_material,
    erase_support_closure,
)
from muninn.review_quiescence import EraseStatus, erase_connection
from muninn.tombstone_key import TOMBSTONE_FILE
from muninn.tombstones import append_tombstones, reapply_tombstones

__all__ = [
    "MIN_MATCH",
    "NOT_COVERED",
    "TOMBSTONE_FILE",
    "EraseRequest",
    "erase",
    "reapply_tombstones",
    "residue_scan",
    "run_erase",
    "run_erase_request",
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


@dataclass(frozen=True)
class EraseRequest:
    """Group one selection and its dry-run intent without validating early."""

    session: str | None = None
    event_ref: str | None = None
    match: str | None = None
    dry_run: bool = False


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
            (
                s["path"].rsplit("#", 1)[0]
                if s["root"] == "cursor-imports"
                else str(roots[s["root"]] / s["path"])
            )
            for s in target.touched.values()
            if s["root"] in roots or s["root"] == "cursor-imports"
        }
    )


def _out_of_scope(env: Mapping[str, str]) -> list[str]:
    """List other derived copies that exist, for the owner to deal with."""
    home = Path(env.get("HOME") or Path.home())
    found = [str(home / rel) for rel in _DERIVED if (home / rel).exists()]
    return sorted(found)


def _aside_remove(aside: list[str]) -> str | None:
    """Return a shell command that removes only the listed literal paths."""
    if not aside:
        return None
    if os.name != "nt":
        return "rm -- " + " ".join(shlex.quote(path) for path in aside)
    literals: list[str] = []
    for path in aside:
        # PowerShell recognises smart single quotes as string delimiters too.
        for quote in "'\u2018\u2019\u201a\u201b":
            path = path.replace(quote, quote * 2)
        literals.append("'" + path + "'")
    return "Remove-Item -LiteralPath @(" + ",".join(literals) + ")"


def _aside_report(home: Path) -> dict[str, object]:
    """Name each set-aside store erase cannot scrub, with the removal."""
    aside = aside_files(home)
    return {"aside_files": aside, "aside_remove": _aside_remove(aside)}


def _apply(conn: sqlite3.Connection, target: Target) -> ErasureSummary:
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
    summary = erase_support_closure(conn, target.review, time.time())
    conn.executemany(  # source_issue and usage rows cascade
        "DELETE FROM source WHERE id = ?", [(i,) for i in target.sources]
    )

    return summary


def _commit(conn: sqlite3.Connection, target: Target) -> ErasureSummary:
    """Apply the plan atomically, or leave the database untouched."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        summary = _apply(conn, target)
        conn.execute("COMMIT")
        return summary
    except BaseException:
        # BaseException: an interrupt mid-transaction must not leave the
        # connection holding the write lock or lose the original failure.
        if conn.in_transaction:
            with suppress(BaseException):
                conn.execute("ROLLBACK")
        raise


def _erase_and_verify(
    conn: sqlite3.Connection,
    home: Path,
    target: Target,
    match: str | None,
    status: EraseStatus,
) -> dict[str, object]:
    """Commit the plan, then measure what survived."""
    needles = [match.encode()] if match else pick_needles(conn, target)
    # Both must be computed before the delete: afterwards the texts and the
    # vocabulary evidence are gone.
    fts_ids = [
        r[0]
        for ident, revision in target.review.versions
        for r in conn.execute(
            "SELECT rowid FROM knowledge_fts WHERE knowledge_id=? AND "
            "revision=?",
            (ident, revision),
        )
    ]
    rare = rare_terms(conn, target.events, fts_ids)
    # Write-ahead: the tombstones reach disk before the commit, so a crash
    # in between cannot lose an erasure.
    # The key is made here, after planning, so a dry run never creates it.
    target.tombstones += content_tombstones(conn, target)
    status.erasure_phase = "intent_may_be_durable"
    append_tombstones(home, target.tombstones)
    status.erasure_phase = "intent_durable"
    summary = _commit(conn, target)
    status.erasure_phase = "committed"
    working_files = sum(
        erase_attempt_material(home, row[0])
        for ident in target.review.requests
        for row in conn.execute(
            "SELECT id FROM review_attempt WHERE request_id=?", (ident,)
        )
    )
    summary = replace(
        summary,
        working_files=working_files,
        retained_archives=len(aside_files(home)),
    )
    # A held reader can prevent truncation; the residue scan then reports WAL.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if not match:  # a needle still in a surviving row is not residue
        needles = [n for n in needles if not still_stored(conn, n)]
    vocab = vocab_left(conn, rare)
    residue = residue_scan(home, needles)
    if not vocab and not residue and not summary.retained_archives:
        status.erasure_phase = "verified"
    return {
        "review": asdict(summary),
        "vocab": vocab,
        "needles": len(needles),
        # The list of leftover files, or 0 when the scan is clean.
        "residue": residue or 0,
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
    caller holds ``store.writer_lock`` and, for real erasure, has established
    producer quiescence through ``erase_connection``. This low-level function
    never acquires those barriers. A ``ValueError`` propagates when
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
    return _erase(
        conn,
        home,
        EraseRequest(session, event_ref, match, dry_run),
        env,
        EraseStatus(),
    )


def _erase(
    conn: sqlite3.Connection,
    home: Path,
    request: EraseRequest,
    env: Mapping[str, str],
    status: EraseStatus,
) -> dict[str, object]:
    """Collect and erase once under the caller's established barriers."""
    session, event_ref, match = (
        request.session,
        request.event_ref,
        request.match,
    )
    dry_run = request.dry_run
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
        "evidence": len(target.review.evidence),
        "knowledge": len(target.knowledge),
        "tombstones": len(target.tombstones) + len(target.content),
        "provider_files": _provider_files(target, ingest.default_roots(env)),
        "out_of_scope": _out_of_scope(env),
        "not_covered": list(NOT_COVERED),
    } | _aside_report(home)
    if dry_run:
        return out
    return out | _erase_and_verify(conn, home, target, match, status)


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
    """Erase through one coordinated root, preserving the selector API."""
    return run_erase_request(
        home,
        EraseRequest(session, event_ref, match, dry_run),
        wait_s=wait_s,
        env=env,
    )


def run_erase_request(
    home: Path,
    request: EraseRequest,
    *,
    wait_s: float = 15.0,
    env: Mapping[str, str] = os.environ,
    status: EraseStatus | None = None,
) -> dict[str, object]:
    """Establish producer quiescence and writer ownership, then erase.

    Real erasure refuses an absent store; dry runs keep their writer-only
    path and may initialize an empty store. Neither creates containment
    evidence. Original callee exceptions propagate with the caller's status.

    Args:
        home: Data directory.
        request: Selector and dry-run intent.
        wait_s: Coordination-acquisition budget, not a whole-command deadline.
        env: Environment, for provider roots and ``HOME``.
        status: Optional caller-owned milestones retained through failures.

    Returns:
        Counts and paths only; never erased text or the match string.
    """
    _pick_mode(request.session, request.event_ref, request.match)
    if request.event_ref is not None:
        query.parse_ref(request.event_ref)
    status = status if status is not None else EraseStatus()
    with ExitStack() as resources:
        try:
            if request.dry_run:
                resources.enter_context(store.writer_lock(home, wait_s=wait_s))
                conn = store.connect_rw(store.db_path(home))
                resources.callback(conn.close)
            else:
                conn = resources.enter_context(
                    erase_connection(home, wait_s=wait_s, status=status)
                )
            return _erase(conn, home, request, env, status)
        except BaseException:
            # Secondary cleanup must not hide the original body failure.
            with suppress(BaseException):
                resources.close()
            raise
