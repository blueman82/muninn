"""Erase revisioned knowledge and its governed support lineage."""

from __future__ import annotations

import math
import os
import re
import shutil
import sqlite3
import sys
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path

from muninn import (
    decision_erase,
    knowledge_erase_links,
    platform_windows,
    store,
)
from muninn.knowledge_erase_state import SupportClosure
from muninn.knowledge_models import AttemptId, EvidenceId
from muninn.tombstone_intent import IntentRow


@dataclass(frozen=True)
class ErasureSummary:
    """Count erased governed material without returning its content."""

    evidence: int
    versions: int
    requests: int
    unresolved: int
    working_files: int
    retained_archives: int


def erase_review_support(
    conn: sqlite3.Connection, evidence_ids: tuple[EvidenceId, ...], now: float
) -> ErasureSummary:
    """Erase selected evidence and every associated governed derivation."""
    return erase_support_closure(
        conn, support_closure(conn, evidence_ids), now
    )


def erase_support_closure(
    conn: sqlite3.Connection, plan: SupportClosure, now: float
) -> ErasureSummary:
    """Apply an already collected closure inside its caller's transaction."""
    if not conn.in_transaction:
        raise ValueError("caller_transaction_required")
    if type(now) is not float or not math.isfinite(now) or now < 0:
        raise ValueError("invalid_time")
    evidence = 0
    for ident in plan.evidence:
        evidence += conn.execute(
            "UPDATE review_evidence SET excerpt=NULL,state='erased' "
            "WHERE id=? AND state!='erased'",
            (ident,),
        ).rowcount
    decision_erase.scrub(conn, plan, now)
    versions = _scrub_versions(conn, plan, now)
    _fence_requests(conn, plan, now)
    unresolved = 0
    for ident in plan.unresolved:
        unresolved += conn.execute(
            "UPDATE unresolved SET item_json=NULL,state='erased' "
            "WHERE id=? AND state!='erased'",
            (ident,),
        ).rowcount
    return ErasureSummary(
        evidence, versions, len(plan.requests), unresolved, 0, 0
    )


def support_closure(
    conn: sqlite3.Connection,
    evidence_ids: tuple[EvidenceId, ...],
    *,
    request_ids: tuple[str, ...] = (),
    versions: tuple[tuple[str, int], ...] = (),
    unresolved_ids: tuple[str, ...] = (),
    candidate_ids: tuple[str, ...] = (),
    decision_ids: tuple[str, ...] = (),
) -> SupportClosure:
    """Collect transitive associations without changing the store."""
    plan = SupportClosure(
        set(evidence_ids),
        set(versions),
        set(request_ids),
        set(unresolved_ids),
        set(candidate_ids),
        set(decision_ids),
    )
    while True:
        before = plan.sizes()
        knowledge_erase_links.collect_links(conn, plan)
        decision_erase.collect_links(conn, plan)
        if before == plan.sizes():
            return plan


def _scrub_versions(
    conn: sqlite3.Connection, plan: SupportClosure, now: float
) -> int:
    """Withdraw whole affected revisions instead of inventing partial text."""
    count = 0
    for ident, revision in sorted(plan.versions):
        count += conn.execute(
            "UPDATE knowledge_version SET content_json=NULL,reason=NULL,"
            "expiry_json=NULL,erased_at=? WHERE knowledge_id=? AND revision=? "
            "AND erased_at IS NULL",
            (now, ident, revision),
        ).rowcount
        conn.execute(
            "UPDATE knowledge_claim SET text=NULL,dependency_keys_json='[]',"
            "state='erased' WHERE knowledge_id=? AND revision=? "
            "AND (state!='erased' OR text IS NOT NULL OR "
            "dependency_keys_json!='[]')",
            (ident, revision),
        )
        conn.execute(
            "UPDATE knowledge SET state=CASE WHEN current_revision=? THEN "
            "'erased' "
            "ELSE state END,retracted_reason=NULL WHERE id=? "
            "AND (retracted_reason IS NOT NULL OR (current_revision=? AND "
            "state!='erased'))",
            (revision, ident, revision),
        )
        conn.execute(
            "DELETE FROM knowledge_fts WHERE knowledge_id=? AND revision=?",
            (ident, revision),
        )
    return count


def _fence_requests(
    conn: sqlite3.Connection, plan: SupportClosure, now: float
) -> None:
    """Fence all attempts while preserving identities and uncertain charges."""
    for ident in sorted(plan.requests):
        conn.execute(
            "UPDATE review_request SET state='cancelled',retry_at=NULL,"
            "current_attempt_id=NULL,cancel_code='evidence_erased' WHERE id=? "
            "AND (state!='cancelled' OR retry_at IS NOT NULL OR "
            "current_attempt_id "
            "IS NOT NULL OR cancel_code IS NOT 'evidence_erased')",
            (ident,),
        )
        conn.execute(
            "UPDATE review_attempt SET "
            "ended_at=?,progress_at=?,outcome='evidence_erased' "
            "WHERE request_id=? AND ended_at IS NULL",
            (now, now, ident),
        )
        conn.execute(
            "UPDATE review_call SET "
            "state='unknown',ended_at=?,outcome='evidence_erased' "
            "WHERE attempt_id IN (SELECT id FROM review_attempt WHERE "
            "request_id=?) "
            "AND state IN ('reserved','launched')",
            (now, ident),
        )
        conn.execute(
            "UPDATE review_receipt SET no_change_reason=NULL WHERE "
            "request_id=? AND no_change_reason IS NOT NULL",
            (ident,),
        )


def erase_attempt_material(home: Path, attempt_id: AttemptId) -> int:
    """Remove only an attempt's governed directory without following links.

    The caller must contain file producers before removal. A missing directory
    returns zero. Each removed file or symlink counts once; directories do not.
    Filesystem failures propagate, so a partial removal cannot claim success.

    Args:
        home: Governed data directory.
        attempt_id: Program-owned attempt identity.

    Returns:
        Number of removed file entries.

    Raises:
        ValueError: If the attempt identity is invalid.
        OSError: If the owned path is unsafe or removal fails.
    """
    if re.fullmatch(r"A_[0-9a-f]{32}", attempt_id) is None:
        raise ValueError("invalid_attempt_id")
    parent = home / "review-work"
    path = parent / attempt_id
    if any(p.is_symlink() or p.is_junction() for p in (parent, path)):
        raise OSError("unsafe_attempt_path")
    if not path.exists():
        return 0
    if not path.is_dir() or path.resolve().parent != parent.resolve():
        raise OSError("unsafe_attempt_path")
    with closing(store.connect_ro(store.db_path(home))) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM review_attempt WHERE id=?", (attempt_id,)
            ).fetchone()
            is None
        ):
            raise OSError("unknown_attempt")
    if os.name != "nt":
        return _remove_attempt_posix(home, attempt_id)
    platform_windows.assert_ancestry(path)
    count = 0
    for root, dirs, files in path.walk(follow_symlinks=False):
        junctions = [name for name in dirs if (root / name).is_junction()]
        dirs[:] = [name for name in dirs if name not in junctions]
        count += len(files) + len(junctions)
    shutil.rmtree(path)
    return count


def _unlink_children(fd: int) -> int:
    """Unlink entries relative to held directories, never through symlinks."""
    if sys.platform == "win32":
        raise OSError("attempt_erasure_platform")
    count = 0
    with os.scandir(fd) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                try:
                    count += _unlink_children(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=fd)
            else:
                os.unlink(entry.name, dir_fd=fd)
                count += 1
    return count


def _remove_attempt_posix(home: Path, attempt_id: AttemptId) -> int:
    """Bind every parent descriptor so a swapped pathname cannot escape."""
    if sys.platform == "win32":
        raise OSError("attempt_erasure_platform")
    with ExitStack() as stack:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(home, flags)
        stack.callback(os.close, parent)
        parent = os.open("review-work", flags, dir_fd=parent)
        stack.callback(os.close, parent)
        attempt = os.open(attempt_id, flags, dir_fd=parent)
        stack.callback(os.close, attempt)
        count = _unlink_children(attempt)
        os.rmdir(attempt_id, dir_fd=parent)
        return count


def plan_replay_support(
    conn: sqlite3.Connection, rows: tuple[IntentRow, ...]
) -> SupportClosure:
    """Collect governed targets from the complete pre-insertion intent."""
    plan = knowledge_erase_links.intent_plan(conn, rows)
    decision_erase.collect_intents(conn, plan, rows)
    return support_closure(
        conn,
        tuple(plan.evidence),
        request_ids=tuple(plan.requests),
        decision_ids=tuple(plan.decisions),
    )


def replay_support(conn: sqlite3.Connection, now: float) -> SupportClosure:
    """Scrub recorded support erasures inside the caller's transaction."""
    rows = tuple(
        IntentRow(*r)
        for r in conn.execute(
            "SELECT provider,level,session_root,thread_id,line,line_sha256,"
            "created_at FROM tombstone"
        )
    )
    plan = plan_replay_support(conn, rows)
    erase_support_closure(conn, plan, now)
    return plan
