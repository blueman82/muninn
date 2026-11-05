"""Append managed execution history under live reservation fences."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Literal

from muninn import review_budget
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext
from muninn.review_progress import digest, require_transaction
from muninn.review_read_budget import ReadBudget
from muninn.review_runtime_models import (
    ActivationEvent,
    ManagedCallRecord,
    RuntimeIdentity,
)


def _identity_digest(identity: RuntimeIdentity) -> str:
    """Identify one full checked runtime fact set without source text."""
    return hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def identity_digest(identity: RuntimeIdentity) -> str:
    """Return the immutable execution digest used by wrapper packets."""
    return _identity_digest(identity)


def _read(
    conn: sqlite3.Connection, context: ReviewContext, now: float
) -> tuple[str, ManagedCallRecord]:
    """Require an owned unlaunched reservation under current authority."""
    require_transaction(conn, now)
    review_budget.check_context(
        conn, context, budget=ReadBudget(context.policy.max_input_bytes)
    )
    row = conn.execute(
        "SELECT requested_json FROM review_call WHERE attempt_id=? AND round_"
        "index=? "
        "AND state='reserved' AND launched_at IS NULL AND ended_at IS NULL",
        (context.attempt_id, context.round_index),
    ).fetchone()
    if row is None:
        raise ValueError("runtime_race_exhausted")
    record = ManagedCallRecord.model_validate_json(row[0])
    if (
        record.selection != context.policy.runner
        or record.launched_identity is not None
    ):
        raise ValueError("runtime_race_exhausted")
    return row[0], record


def _write(
    conn: sqlite3.Connection,
    context: ReviewContext,
    before: str,
    record: ManagedCallRecord,
) -> None:
    """Compare and append without relabeling a started or unknown call."""
    record = ManagedCallRecord.model_validate_json(canonical_json(record))
    changed = conn.execute(
        "UPDATE review_call SET requested_json=? WHERE attempt_id=? AND round"
        "_index=? "
        "AND state='reserved' AND launched_at IS NULL AND ended_at IS NULL AN"
        "D requested_json=?",
        (
            canonical_json(record),
            context.attempt_id,
            context.round_index,
            before,
        ),
    ).rowcount
    if changed != 1:
        raise ValueError("runtime_race_exhausted")


def consume_refresh(
    conn: sqlite3.Connection, context: ReviewContext, now: float
) -> None:
    """Spend the refresh before discovery, including failed qualification."""
    require_transaction(conn, now)
    present = conn.execute(
        "SELECT 1 FROM review_call WHERE attempt_id=? AND round_index=?",
        (context.attempt_id, context.round_index),
    ).fetchone()
    if present is None:
        return
    before, record = _read(conn, context, now)
    if record.refresh_count:
        raise ValueError("runtime_race_exhausted")
    _write(
        conn, context, before, record.model_copy(update={"refresh_count": 1})
    )


def offer_execution(
    conn: sqlite3.Connection,
    context: ReviewContext,
    identity: RuntimeIdentity,
    now: float,
) -> None:
    """Append one offer while consuming at most one refresh."""
    before, record = _read(conn, context, now)
    correct_model = (
        identity.requested.model,
        identity.requested.reasoning,
    ) == (
        record.selection.model,
        record.selection.reasoning,
    )
    correct_root = (
        identity.package_root.parent
        == record.selection.manager_root / "releases"
    )
    if not correct_model or not correct_root:
        raise ValueError("runtime_selection_unsafe")
    latest = record.candidates[-1]
    candidates, refresh = record.candidates, record.refresh_count
    if identity != latest:
        if len(candidates) != 1:
            raise ValueError("runtime_race_exhausted")
        if record.events and record.events[-1].kind != "prestart_refusal":
            raise ValueError("runtime_race_exhausted")
        candidates, refresh = (*candidates, identity), 1
    elif record.events:
        raise ValueError("runtime_race_exhausted")
    event = ActivationEvent(
        kind="offer",
        identity_digest=identity_digest(identity),
        code=None,
        at=now,
    )
    _write(
        conn,
        context,
        before,
        record.model_copy(
            update={
                "candidates": candidates,
                "refresh_count": refresh,
                "events": (*record.events, event),
            }
        ),
    )


def append_prestart_refusal(
    conn: sqlite3.Connection,
    context: ReviewContext,
    identity_digest: str,
    code: Literal["runtime_changed", "exec_enoent"],
    now: float,
) -> None:
    """Record only a positive owned wrapper refusal before native creation."""
    before, record = _read(conn, context, now)
    if (
        not record.events
        or record.events[-1].kind != "offer"
        or record.events[-1].identity_digest != identity_digest
    ):
        raise ValueError("runtime_race_exhausted")
    event = ActivationEvent(
        kind="prestart_refusal",
        identity_digest=identity_digest,
        code=code,
        at=now,
    )
    _write(
        conn,
        context,
        before,
        record.model_copy(update={"events": (*record.events, event)}),
    )


def freeze_launched_execution(
    conn: sqlite3.Connection,
    context: ReviewContext,
    identity_digest: str,
    now: float,
) -> None:
    """Freeze the offered identity once a native creation is verified."""
    require_transaction(conn, now)
    row = conn.execute(
        "SELECT c.requested_json FROM review_call c JOIN review_attempt a "
        "ON a.id=c.attempt_id WHERE c.attempt_id=? AND c.round_index=? "
        "AND a.request_id=? AND a.generation=? AND c.policy_digest=?",
        (
            context.attempt_id,
            context.round_index,
            context.request_id,
            context.generation,
            digest(context.policy),
        ),
    ).fetchone()
    if row is None:
        raise ValueError("runtime_race_exhausted")
    before = row[0]
    record = ManagedCallRecord.model_validate_json(before)
    if (
        record.selection != context.policy.runner
        or not record.events
        or record.events[-1].kind != "offer"
        or record.events[-1].identity_digest != identity_digest
        or not any(
            _identity_digest(candidate) == identity_digest
            for candidate in record.candidates
        )
        or record.launched_identity not in (None, identity_digest)
    ):
        raise ValueError("runtime_race_exhausted")
    if record.launched_identity == identity_digest:
        return
    changed = conn.execute(
        "UPDATE review_call SET requested_json=? WHERE attempt_id=? "
        "AND round_index=? AND requested_json=?",
        (
            canonical_json(
                record.model_copy(
                    update={"launched_identity": identity_digest}
                )
            ),
            context.attempt_id,
            context.round_index,
            before,
        ),
    ).rowcount
    if changed != 1:
        raise ValueError("runtime_race_exhausted")
