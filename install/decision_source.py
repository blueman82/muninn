"""Verify stopped marked-one containment without repairing historical state."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path

from install.context import StepFailedError
from install.policy_source_format import validate_policy_source
from muninn import (
    knowledge_erase_links,
    platform_io,
    review_pause_owner,
    tombstones,
)
from muninn import review_pause as q
from muninn import review_pause_state as state
from muninn.tombstone_intent import read_intent


def verify_source(owner: q.MaintenanceOwner, conn: sqlite3.Connection) -> None:
    """Require complete bound containment and already completed old erasure.

    This read-only check owns no lock, connection or transaction. Interrupted
    old pauses require the verified installed old recovery path before upgrade.

    Args:
        owner: Caller-held lifetime exclusion and one active writer barrier.
        conn: Caller-owned read-only exact marked-one source connection.

    Raises:
        StepFailedError: If historical containment or erasure is unresolved.
    """
    if not owner.active or not owner.writer_held or conn.in_transaction:
        raise StepFailedError("marked_v1_recovery_required")
    if validate_policy_source(conn) != 1:
        raise StepFailedError("cutover_source_mismatch")
    try:
        identity = state.lock_identity(owner.home, owner.fd)
        latest = state.parse(
            state.inspect(owner.home, state.WITNESS)[0], state.Witness
        )
        review_pause_owner.same_owner(owner.selected, latest)
        if (
            latest is None
            or latest.state != "complete"
            or latest.lock_identity != identity
            or state.inspect(owner.home, state.BASELINE)[1] is not None
            or conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
        ):
            raise StepFailedError("marked_v1_recovery_required")
        tombstones.require_current_tombstones(conn, owner.home)
        _completed_support(conn, owner)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise StepFailedError("marked_v1_recovery_required") from exc


def _completed_support(
    conn: sqlite3.Connection, owner: q.MaintenanceOwner
) -> None:
    """Check the complete ordinary closure and postcommit attempt cleanup."""
    plan = knowledge_erase_links.intent_plan(
        conn, read_intent(conn, owner.home).rows
    )
    while knowledge_erase_links.collect_links(conn, plan):
        pass
    for ident in plan.evidence:
        _absent(
            conn,
            "SELECT 1 FROM review_evidence WHERE id=? AND "
            "(state!='erased' OR excerpt IS NOT NULL)",
            (ident,),
        )
    for ident in plan.unresolved:
        _absent(
            conn,
            "SELECT 1 FROM unresolved WHERE id=? AND "
            "(state!='erased' OR item_json IS NOT NULL)",
            (ident,),
        )
    for ident, revision in plan.versions:
        _version_erased(conn, ident, revision)
    for ident in plan.requests:
        _request_erased(conn, ident, owner.home)


def _absent(
    conn: sqlite3.Connection, sql: str, params: tuple[str | int, ...]
) -> None:
    """Refuse a surviving governed body, derived artifact or active fence."""
    if conn.execute(sql, params).fetchone() is not None:
        raise ValueError("erasure_pending")


def _version_erased(
    conn: sqlite3.Connection, ident: str, revision: int
) -> None:
    """Require whole-revision content, index and current-state erasure."""
    _absent(
        conn,
        "SELECT 1 FROM knowledge_version WHERE knowledge_id=? "
        "AND revision=? AND (content_json IS NOT NULL OR reason IS NOT "
        "NULL OR expiry_json IS NOT NULL OR erased_at IS NULL)",
        (ident, revision),
    )
    _absent(
        conn,
        "SELECT 1 FROM knowledge_claim WHERE knowledge_id=? AND "
        "revision=? AND (text IS NOT NULL OR state!='erased' OR "
        "dependency_keys_json!='[]')",
        (ident, revision),
    )
    _absent(
        conn,
        "SELECT 1 FROM knowledge WHERE id=? AND "
        "(retracted_reason IS NOT NULL OR (current_revision=? AND "
        "state!='erased'))",
        (ident, revision),
    )
    _absent(
        conn,
        "SELECT 1 FROM knowledge_fts WHERE knowledge_id=? AND " "revision=?",
        (ident, revision),
    )


def _request_erased(conn: sqlite3.Connection, ident: str, home: Path) -> None:
    """Require ended attempts and no remaining governed working path."""
    _absent(
        conn,
        "SELECT 1 FROM review_request WHERE id=? AND "
        "(state!='cancelled' OR current_attempt_id IS NOT NULL OR "
        "retry_at IS NOT NULL OR cancel_code IS NOT 'evidence_erased')",
        (ident,),
    )
    _absent(
        conn,
        "SELECT 1 FROM review_receipt WHERE request_id=? AND "
        "no_change_reason IS NOT NULL",
        (ident,),
    )
    _absent(
        conn,
        "SELECT 1 FROM review_attempt WHERE request_id=? AND "
        "ended_at IS NULL",
        (ident,),
    )
    _absent(
        conn,
        "SELECT 1 FROM review_call c JOIN review_attempt a ON "
        "a.id=c.attempt_id WHERE a.request_id=? AND "
        "c.state IN ('reserved','launched')",
        (ident,),
    )
    for row in conn.execute(
        "SELECT id FROM review_attempt WHERE request_id=?", (ident,)
    ):
        _working_absent(home, row[0])


def _working_absent(home: Path, attempt: str) -> None:
    """Refuse remaining paths without following links or deleting files."""
    if re.fullmatch(r"A_[0-9a-f]{32}", attempt) is None:
        raise ValueError("erasure_pending")
    parent = home / "review-work"
    try:
        found = parent.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(found.st_mode)
        or parent.is_junction()
        or not platform_io.is_private(parent, directory=True)
        or (os.name != "nt" and found.st_uid != os.getuid())
    ):
        raise ValueError("erasure_pending")
    try:
        (parent / attempt).lstat()
    except FileNotFoundError:
        return
    raise ValueError("erasure_pending")
