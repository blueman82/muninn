"""Accept one historical unknown through verified exclusive maintenance."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from muninn import review_pause as pause
from muninn import review_runtime_authority, review_unknown_binding, store
from muninn import review_unknown_state as state
from muninn.knowledge_models import AttemptId, Revision, UtcSeconds
from muninn.obs_log import log_call
from muninn.review_codex_launch import stored_binding
from muninn.review_codex_policy import check_host
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_launch_inputs import load_capability
from muninn.review_models import ReviewPolicy
from muninn.review_progress import checked_policy, digest
from muninn.review_runner import RunnerCapability
from muninn.review_runtime_bridge import current_bridge, effective_policy
from muninn.review_runtime_models import ManagedCodexSelection
from muninn.review_unknown_state import UnknownAcknowledgement


def _policy(conn: sqlite3.Connection, home: Path) -> tuple[ReviewPolicy, str]:
    """Validate current v3 authority without creating or renewing a grant."""
    control = conn.execute("SELECT * FROM review_control").fetchone()
    if control is None or control["policy_json"] is None:
        raise ValueError("recovery_refused")
    policy = checked_policy(
        ReviewPolicy.model_validate_json(control["policy_json"])
    )
    if (
        digest(policy) != control["policy_digest"]
        or policy.policy_id != "muninn-codex-ongoing-production-v3"
        or policy.experimental_codex is None
        or policy.runner.runner != "codex_cli"
    ):
        raise ValueError("recovery_refused")
    check_host(policy.experimental_codex, home)
    authority: tuple[str, ...] = ()
    if isinstance(policy.runner, ManagedCodexSelection):
        capability = review_runtime_authority.selected_capability(
            home, digest(policy)
        )
        binding, approval = review_runtime_authority.checked_binding(
            home, capability
        )
        if (
            binding.requested != policy.runner
            or approval.new_policy_digest != digest(policy)
        ):
            raise ValueError("recovery_refused")
        bridge = current_bridge(conn, policy)
        authority = (
            canonical_json(capability),
            canonical_json(binding),
            canonical_json(approval),
            canonical_json(bridge) if bridge is not None else "",
        )
    else:
        legacy = load_capability(home, policy)
        if not isinstance(legacy, RunnerCapability):
            raise ValueError("recovery_refused")
        legacy_binding = stored_binding(home, legacy)
        if (
            legacy_binding.requested != policy.runner
            or legacy_binding.host_identity
            != policy.experimental_codex.host_identity
        ):
            raise ValueError("recovery_refused")
        authority = (canonical_json(legacy), canonical_json(legacy_binding))
    return (
        policy,
        hashlib.sha256(canonical_json_value(authority).encode()).hexdigest(),
    )


def _snapshot(
    conn: sqlite3.Connection, home: Path, attempt: AttemptId, round_index: int
) -> tuple[ReviewPolicy | None, tuple[str, ...]]:
    """Freeze selected history, control and any new-decision authority."""
    rows = review_unknown_binding.snapshots(conn, attempt, round_index)
    control = conn.execute("SELECT * FROM review_control").fetchone()
    previous = state.read(conn, attempt, round_index)
    if previous is not None:
        if not review_unknown_binding.matches(conn, previous):
            raise ValueError("recovery_refused")
        policy, authority = None, canonical_json(previous)
    else:
        policy, authority = _policy(conn, home)
    return policy, (
        *(review_unknown_binding.row_digest(row) for row in rows),
        review_unknown_binding.row_digest(control),
        authority,
    )


def _apply(
    conn: sqlite3.Connection,
    home: Path,
    attempt: AttemptId,
    round_index: int,
    now: float,
    expected: tuple[str, ...],
) -> UnknownAcknowledgement:
    """Commit acceptance, generation fence and admission as one transition."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        policy, snapshot = _snapshot(conn, home, attempt, round_index)
        if snapshot != expected:
            raise ValueError("recovery_refused")
        previous = state.read(conn, attempt, round_index)
        if previous is not None:
            if not review_unknown_binding.matches(conn, previous):
                raise ValueError("recovery_refused")
            conn.execute("ROLLBACK")
            return previous
        assert policy is not None
        control = conn.execute("SELECT * FROM review_control").fetchone()
        call, parent, request = review_unknown_binding.snapshots(
            conn, attempt, round_index
        )
        if (
            control["admission"] != 0
            or parent["generation"] > control["generation"]
            or review_unknown_binding.unsafe_calls(
                conn, selected=(attempt, round_index)
            )
            or conn.execute(
                "SELECT 1 FROM review_attempt WHERE ended_at IS NULL "
                "UNION ALL "
                "SELECT 1 FROM review_request WHERE state='running' LIMIT 1"
            ).fetchone()
            is not None
        ):
            raise ValueError("recovery_refused")
        old_generation = control["generation"]
        conn.execute(
            "UPDATE review_control SET admission=1,generation=generation+1"
        )
        effective = effective_policy(conn, request)
        if effective != policy or call["policy_digest"] not in {
            digest(effective),
            request["policy_digest"],
        }:
            raise ValueError("recovery_refused")
        acknowledgement = UnknownAcknowledgement(
            acknowledgement_id=f"UA_{uuid4().hex}",
            attempt_id=attempt,
            round_index=round_index,
            request_id=request["id"],
            call_digest=review_unknown_binding.row_digest(call),
            attempt_digest=review_unknown_binding.row_digest(parent),
            request_digest=review_unknown_binding.row_digest(request),
            request_policy_digest=request["policy_digest"],
            call_policy_digest=call["policy_digest"],
            policy_digest=digest(policy),
            old_generation=old_generation,
            resulting_generation=old_generation + 1,
            acknowledged_at=now,
            accept_uncertainty=True,
        )
        state.save(conn, acknowledgement)
        conn.execute("COMMIT")
        return acknowledgement
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def recover(
    home: Path,
    attempt: AttemptId,
    round_index: int,
    *,
    accept_uncertainty: bool,
    now: float,
) -> UnknownAcknowledgement:
    """Accept one named uncertainty while preserving all failed history.

    Args:
        home: Existing private governed store directory.
        attempt: Exact historical attempt identity.
        round_index: One-based call round.
        accept_uncertainty: Explicit owner acceptance, required to be true.
        now: Finite acknowledgement time.

    Returns:
        The immutable original acknowledgement, including on repetition.

    Raises:
        ValueError: If acceptance, authority, history or containment fails.
        OSError: If required private authority files cannot be checked.
    """
    attempt = TypeAdapter(AttemptId).validate_python(attempt, strict=True)
    TypeAdapter(Revision).validate_python(round_index, strict=True)
    TypeAdapter(UtcSeconds).validate_python(now, strict=True)
    if accept_uncertainty is not True:
        raise ValueError("recovery_refused")
    try:
        with (
            pause.maintenance_owner(
                home, wait_s=5, status=pause.EraseStatus()
            ) as owner,
            pause.maintenance_writer(owner),
            closing(store.connect_rw(store.db_path(home))) as conn,
        ):
            _, expected = _snapshot(conn, home, attempt, round_index)
            pause.verify_current(owner, conn)
            acknowledgement = _apply(
                conn, home, attempt, round_index, now, expected
            )
    except ValueError:
        raise ValueError("recovery_refused") from None
    log_call(
        home,
        {
            "event": "review_unknown_accepted",
            "attempt_id": attempt,
            "request_id": acknowledgement.request_id,
            "round_index": round_index,
            "generation": acknowledgement.resulting_generation,
        },
    )
    return acknowledgement
