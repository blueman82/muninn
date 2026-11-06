"""Resolve only the single consented finite legacy-to-managed queue bridge."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from muninn import review_runtime_authority
from muninn.review_codex_policy import CodexExperiment, ManagedCodexExperiment
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import ReviewPolicy
from muninn.review_progress import checked_policy, digest
from muninn.review_runtime_models import (
    BridgeRequest,
    HostEnrollment,
    InitialBridge,
    ManagedCodexSelection,
    RunnerConfig,
)

_IMMUTABLE = (
    "id",
    "duplicate_key",
    "provider",
    "thread_id",
    "session_root",
    "from_line",
    "through_line",
    "boundary_hash",
    "scope_json",
    "policy_json",
    "policy_digest",
    "trigger",
    "origin",
    "created_at",
    "candidate_id",
    "decision_revision",
)


def migration_delta(old: ReviewPolicy, new: ReviewPolicy) -> None:
    """Permit only the explicit runner and physical-host enrollment delta."""
    previous, selected = old.experimental_codex, new.experimental_codex
    if (
        not isinstance(old.runner, RunnerConfig)
        or old.runner.runner != "codex_cli"
        or not isinstance(new.runner, ManagedCodexSelection)
        or not isinstance(previous, CodexExperiment)
        or not isinstance(selected, ManagedCodexExperiment)
        or old.model_dump(exclude={"runner", "experimental_codex"})
        != new.model_dump(exclude={"runner", "experimental_codex"})
        or previous.model_dump(exclude={"kind", "host_identity"})
        != selected.model_dump(exclude={"kind", "host_identity"})
        or (old.runner.model, old.runner.reasoning)
        != (new.runner.model, new.runner.reasoning)
    ):
        raise ValueError("review_policy_required")


def metadata_digest(row: sqlite3.Row) -> str:
    """Hash request metadata without copying evidence or source text."""
    return hashlib.sha256(
        canonical_json_value({key: row[key] for key in _IMMUTABLE}).encode()
    ).hexdigest()


def _inputs(
    conn: sqlite3.Connection, request: str
) -> tuple[tuple[str, str], ...]:
    """Fingerprint the complete original evidence metadata without excerpts."""
    values: list[tuple[str, str]] = []
    for row in conn.execute(
        "SELECT e.* FROM review_evidence e JOIN review_input i ON i.evidence_"
        "id=e.id "
        "WHERE i.request_id=? ORDER BY e.id",
        (request,),
    ):
        if len(values) >= 4096 or row["state"] != "live":
            raise ValueError("review_policy_required")
        data = {key: row[key] for key in dict(row) if key != "excerpt"}
        values.append(
            (
                row["id"],
                hashlib.sha256(
                    canonical_json_value(data).encode()
                ).hexdigest(),
            )
        )
    return tuple(values)


def capture(
    conn: sqlite3.Connection,
    old: ReviewPolicy,
    new: ReviewPolicy,
    *,
    host_enrollment: HostEnrollment,
) -> InitialBridge:
    """Capture the full old queue in the caller's coherent snapshot."""
    migration_delta(old, new)
    assert isinstance(old.experimental_codex, CodexExperiment)
    assert isinstance(new.experimental_codex, ManagedCodexExperiment)
    if (
        host_enrollment.saved_legacy_host
        != old.experimental_codex.host_identity
        or host_enrollment.target != new.experimental_codex.host_identity
    ):
        raise ValueError("review_policy_required")
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    requests: list[BridgeRequest] = []
    metadata_bytes = 0
    for row in conn.execute(
        "SELECT r.* FROM review_request r WHERE r.policy_digest=? "
        "AND r.state IN ('pending','running','waiting') AND NOT EXISTS "
        "(SELECT 1 FROM review_receipt t WHERE t.request_id=r.id) ORDER BY r."
        "id LIMIT 1025",
        (digest(old),),
    ):
        if (
            len(requests) >= 1024
            or row["candidate_id"] is not None
            or row["policy_json"] != canonical_json(old)
        ):
            raise ValueError("review_policy_required")
        requests.append(
            BridgeRequest(
                request_id=row["id"],
                metadata_digest=metadata_digest(row),
                inputs=_inputs(conn, row["id"]),
            )
        )
        metadata_bytes += len(canonical_json(requests[-1]).encode())
        if metadata_bytes > 256 * 1024:
            raise ValueError("runtime_structure_limit")
    capsule = InitialBridge(
        kind="codex-managed-initial-bridge-v1",
        home=path,
        old_policy_digest=digest(old),
        new_policy_digest=digest(new),
        host_enrollment=host_enrollment,
        requests=tuple(requests),
    )
    if len(canonical_json(capsule).encode()) > 256 * 1024:
        raise ValueError("runtime_structure_limit")
    return capsule


def current_bridge(
    conn: sqlite3.Connection, current: ReviewPolicy
) -> InitialBridge | None:
    """Authenticate one capsule through the current activated stable proof."""
    if not isinstance(current.runner, ManagedCodexSelection):
        return None
    home = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    capability = review_runtime_authority.selected_capability(
        home, digest(current)
    )
    binding, approval = review_runtime_authority.checked_binding(
        home, capability
    )
    if (
        binding.requested != current.runner
        or approval.new_policy_digest != digest(current)
        or not isinstance(current.experimental_codex, ManagedCodexExperiment)
        or binding.host_identity != current.experimental_codex.host_identity
    ):
        raise ValueError("review_policy_required")
    if binding.initial_bridge is None:
        return None
    raw = review_runtime_authority.artifact(
        home, "managed-bridges", binding.initial_bridge
    )
    capsule = InitialBridge.model_validate_json(raw)
    if (
        canonical_json(capsule).encode() != raw
        or capsule.home != home
        or capsule.new_policy_digest != digest(current)
        or capsule.old_policy_digest != approval.old_policy_digest
        or capsule.host_enrollment != approval.host_enrollment
    ):
        raise ValueError("review_policy_required")
    return capsule


def effective_policy(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> ReviewPolicy:
    """Return current authority only for its snapshot or initial capsule."""
    old = checked_policy(ReviewPolicy.model_validate_json(row["policy_json"]))
    control = conn.execute("SELECT * FROM review_control").fetchone()
    if (
        not control["admission"]
        or control["policy_json"] is None
        or digest(old) != row["policy_digest"]
    ):
        raise ValueError("review_policy_required")
    current = checked_policy(
        ReviewPolicy.model_validate_json(control["policy_json"])
    )
    if digest(current) != control["policy_digest"]:
        raise ValueError("review_policy_required")
    if canonical_json(current) == row["policy_json"]:
        return current
    migration_delta(old, current)
    try:
        capsule = current_bridge(conn, current)
    except (ValueError, OSError):
        raise ValueError("review_policy_required") from None
    if capsule is None or capsule.old_policy_digest != digest(old):
        raise ValueError("review_policy_required")
    assert isinstance(old.experimental_codex, CodexExperiment)
    if (
        capsule.host_enrollment.saved_legacy_host
        != old.experimental_codex.host_identity
    ):
        raise ValueError("review_policy_required")
    selected = next(
        (item for item in capsule.requests if item.request_id == row["id"]),
        None,
    )
    if selected is None or selected.metadata_digest != metadata_digest(row):
        raise ValueError("review_policy_required")
    actual = dict(_inputs(conn, row["id"]))
    if any(actual.get(ident) != hashed for ident, hashed in selected.inputs):
        raise ValueError("review_policy_required")
    return current


def eligible_ids(
    conn: sqlite3.Connection, current: ReviewPolicy
) -> tuple[str, ...]:
    """Expose only bounded capsule IDs for indexed supervisor selection."""
    capsule = current_bridge(conn, current)
    return (
        tuple(item.request_id for item in capsule.requests) if capsule else ()
    )
