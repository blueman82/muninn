"""Construct initial stable managed artifacts without publishing authority."""

from __future__ import annotations

import hashlib
from pathlib import Path

from install.codex_interface_models import InterfaceApproval, InterfacePlan
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_progress import digest
from muninn.review_rounds import round_schema
from muninn.review_runtime_authority import fingerprint
from muninn.review_runtime_models import (
    EnrollmentApproval,
    ManagedCapability,
    ManagedCodexSelection,
    ManagedInterfaceBinding,
)
from tools.codex_policy_artifacts import (
    ArtifactPlan,
    PreparedFile,
    recipe_digest,
)


def plan_artifacts(
    plan: InterfacePlan, approval: InterfaceApproval, operation: str
) -> ArtifactPlan:
    """Bind one approved rule to its preserved old auth and queue facts."""
    if approval.plan_digest != plan.digest or not isinstance(
        plan.candidate.runner, ManagedCodexSelection
    ):
        raise ValueError("codex_interface_approval_required")
    capsule = canonical_json(plan.bridge).encode()
    bridge_digest = hashlib.sha256(capsule).hexdigest()
    previous = plan.old_binding
    enrolled = EnrollmentApproval(
        kind="codex-managed-enrollment-v1",
        old_policy_digest=digest(plan.old),
        new_policy_digest=digest(plan.candidate),
        bridge_digest=bridge_digest,
        host_identity=previous.host_identity,
        auth_home=previous.auth_home,
        operation_id=operation,
        plan_digest=plan.digest,
        approved_at=approval.approved_at,
        approval="owner_yes",
    )
    enrolled_raw = canonical_json(enrolled).encode()
    enrolled_digest = hashlib.sha256(enrolled_raw).hexdigest()
    binding = ManagedInterfaceBinding(
        kind="codex-managed-binding-v1",
        requested=plan.candidate.runner,
        host_identity=previous.host_identity,
        auth_home=previous.auth_home,
        cli_cwd=previous.cli_cwd,
        schema_path=previous.schema_path,
        output_contract_digest=previous.output_contract_digest,
        enrollment_approval=enrolled_digest,
        initial_bridge=bridge_digest,
    )
    raw = canonical_json(binding).encode()
    proof = Path("review-capabilities") / (
        hashlib.sha256(raw).hexdigest() + ".json"
    )
    capability = ManagedCapability(
        kind="codex-managed-capability-v1",
        requested=plan.candidate.runner,
        proof_path=plan.home / proof,
        schema_digest=hashlib.sha256(
            canonical_json_value(round_schema()).encode()
        ).hexdigest(),
        config_digest=fingerprint(binding),
    )
    items = {
        proof: raw,
        Path("review-capabilities/selected")
        / (digest(plan.candidate) + ".json"): canonical_json(
            capability
        ).encode(),
        Path("review-capabilities/managed-bridges")
        / (bridge_digest + ".json"): capsule,
        Path("review-capabilities/managed-approvals")
        / (enrolled_digest + ".json"): enrolled_raw,
    }
    files = tuple(
        PreparedFile(path, value) for path, value in sorted(items.items())
    )
    if any(len(item.raw) > 256 * 1024 for item in files):
        raise ValueError("runtime_structure_limit")
    return ArtifactPlan(files, digest(plan.candidate), recipe_digest(files))
