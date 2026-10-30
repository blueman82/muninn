"""Capture a checked host-only proposal without publishing or stopping work."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import cast

from install import host_renewal_scan, policy_preview
from install import legacy_baseline as baseline
from install.context import Ctx, StepFailedError
from install.host_renewal_bindings import BindingInputs, installation
from install.host_renewal_files import lock_identity
from install.host_renewal_models import ContainmentPlan, HostPlan
from install.host_renewal_operation import HeldOperation
from install.host_renewal_scan import StateScan as StateScan
from install.legacy_state import OperationCheck
from install.policy_record_models import Payload
from install.review_policy import read_policy
from install.review_policy_parse import policy as parse_policy
from muninn import review_codex_launch, review_runner, store, store_fingerprint
from muninn.knowledge_models import Digest
from muninn.review_codex_launch import checked_binding
from muninn.review_codex_policy import check_host
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import ReviewPolicy
from muninn.review_pause_state import WITNESS
from muninn.review_rounds import round_schema
from muninn.tombstone_key import KEY_FILE, TOMBSTONE_FILE
from tools.codex_policy_artifacts import ArtifactPlan, plan_artifacts

METADATA_SCAN_TIMEOUT_S = 30.0


def scan_state(
    conn: sqlite3.Connection,
    path: Path,
    *,
    candidate_digest: Digest,
    containment: ContainmentPlan | None = None,
    check: OperationCheck | None = None,
) -> StateScan:
    """Scan the caller's fresh guarded snapshot without taking ownership.

    Args:
        conn: Caller-owned readable snapshot.
        path: Guarded current database path.
        candidate_digest: Digest forbidden in every saved request.
        containment: Named predecessors to retain after they end.
        check: Optional caller-owned installer operation check.

    Returns:
        Complete hashes, sorted IDs and the exact no-call projection.
    """
    return host_renewal_scan.scan(
        conn,
        path,
        candidate_digest=candidate_digest,
        containment=containment,
        check=check,
        timeout=METADATA_SCAN_TIMEOUT_S,
    )


def _digest(value: object) -> str:
    """Hash canonical JSON fields without exposing their private bodies."""
    return hashlib.sha256(canonical_json_value(value).encode()).hexdigest()


def _payload(raw: str, policy: ReviewPolicy) -> Payload:
    """Preserve exact selected bytes separately from canonical authority."""
    canonical = canonical_json(policy)
    return {
        "raw": raw,
        "raw_digest": hashlib.sha256(raw.encode()).hexdigest(),
        "canonical": canonical,
        "digest": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _policies(
    raw: str, old_raw: str, ctx: Ctx
) -> tuple[ReviewPolicy, ReviewPolicy]:
    """Require only the saved host field to change."""
    old = parse_policy(old_raw)
    candidate = parse_policy(raw)
    before, after = old.experimental_codex, candidate.experimental_codex
    if (
        before is None
        or after is None
        or before.host_identity == after.host_identity
    ):
        raise StepFailedError("host_renewal_policy_changed")
    expected = old.model_dump(mode="json")
    expected_terms = cast(dict[str, object], expected["experimental_codex"])
    expected_terms["host_identity"] = after.host_identity
    if (
        expected != candidate.model_dump(mode="json")
        or canonical_json(old) != old_raw
    ):
        raise StepFailedError("host_renewal_policy_changed")
    check_host(after, ctx.data)
    return old, candidate


def artifacts(
    ctx: Ctx, old: ReviewPolicy, candidate: ReviewPolicy
) -> tuple[ArtifactPlan, dict[str, object]]:
    """Check retained artifacts and build the recipe outside writers."""
    home = ctx.data
    selected = (
        home
        / "review-capabilities/selected"
        / (_digest(old.model_dump(mode="json")) + ".json")
    )
    for directory in (
        home,
        selected.parent.parent,
        selected.parent,
        home / "review-prompts",
    ):
        review_runner.check_private_path(directory, directory=True)
    capability = review_runner.RunnerCapability.model_validate_json(
        review_runner.read_private_file(selected, 1024 * 1024)
    )
    binding, target = checked_binding(home, capability)
    terms = old.experimental_codex
    assert terms is not None
    _, executable_digest = review_codex_launch.verify_codex_executable(
        Path(old.runner.executable)
    )
    if (
        binding.requested != old.runner
        or binding.host_identity != terms.host_identity
        or executable_digest != binding.executable_sha256
        or capability.status != "unverified"
        or capability.schema_digest != _digest(round_schema())
        or any(
            getattr(capability, key) != value
            for key, value in old.runner.model_dump().items()
        )
    ):
        raise StepFailedError("host_renewal_artifacts_changed")
    prompt_path = home / "review-prompts" / (old.prompt_digest + ".json")
    prompt = review_runner.read_private_file(prompt_path, old.max_input_bytes)
    current_terms = candidate.experimental_codex
    assert current_terms is not None
    updated = review_codex_launch.ExperimentalBinding.model_validate(
        binding.model_dump() | {"host_identity": current_terms.host_identity}
    )
    config_digest = review_codex_launch.fingerprint(updated)
    artifacts = plan_artifacts(candidate, prompt, updated, config_digest)
    inputs: dict[str, object] = {
        "artifacts": {
            str(path): baseline.file_state(path, private=True)
            for path in (
                selected,
                capability.proof_path,
                binding.schema_path,
                prompt_path,
            )
        },
        "cwd": baseline.directory(binding.cli_cwd, private=True),
        "auth": baseline.directory(binding.auth_home),
        "executable": {
            "target": str(target),
            "digest": executable_digest,
            "file": baseline.file_state(target),
        },
        "binding": updated.model_dump(mode="json"),
        "config_digest": config_digest,
    }
    return artifacts, inputs


def protected_files(ctx: Ctx) -> tuple[object, ...]:
    """Bind fixed external files across signature and configuration checks."""
    states = tuple(
        (
            baseline.file_state(ctx.data / name, private=True)
            if os.path.lexists(ctx.data / name)
            else None
        )
        for name in (KEY_FILE, TOMBSTONE_FILE, WITNESS)
    )
    return (*states, lock_identity(ctx.data, lambda: None))


def preview(
    ctx: Ctx, repo: Path, sha: str, *, operation: HeldOperation | None = None
) -> HostPlan:
    """Read and bind the exact current-format host-only proposal.

    Args:
        ctx: Existing installation and explicit candidate selection.
        repo: Canonical source repository for the exact target release.
        sha: Exact target commit, retained in the plan digest.
        operation: Actual held installer operation for the one lock projection.

    Returns:
        A checked private proposal that grants no host authority.

    Raises:
        StepFailedError: If input, source or identity checks refuse.
    """
    if ctx.platform != "darwin":
        raise StepFailedError("host_renewal_unsupported")
    if (
        not ctx.upgrade
        or ctx.fresh
        or ctx.enable_learning
        or ctx.learning_mode is not None
        or ctx.review_policy is None
    ):
        raise StepFailedError("host_renewal_arguments_invalid")
    path = store.db_path(ctx.data)
    try:
        raw = read_policy(ctx.review_policy)
        candidate = parse_policy(raw)
        candidate_digest = hashlib.sha256(
            canonical_json(candidate).encode()
        ).hexdigest()
        with policy_preview.connection(path) as conn:
            before = store_fingerprint.capture_file(path)
            protected = protected_files(ctx)
            scanned = scan_state(conn, path, candidate_digest=candidate_digest)
        old, candidate = _policies(
            raw, scanned.state.control["policy_json"], ctx
        )
        recipe, artifact_inputs = artifacts(ctx, old, candidate)
        captured = installation(ctx, repo, sha, operation)
        if (
            captured["target_contract"]
            != cast(dict[str, object], artifact_inputs["binding"])[
                "output_contract_digest"
            ]
        ):
            raise StepFailedError("host_renewal_artifacts_changed")
        fields = {
            "kind": "codex_host_renewal_v1",
            "home": str(ctx.home),
            "repo": str(repo),
            "sha": sha,
            "old": _payload(scanned.state.control["policy_json"], old),
            "candidate": _payload(raw, candidate),
            "state": scanned.state.model_dump(mode="json"),
            "bindings_digest": _digest(
                {
                    "installation": captured,
                    "artifacts": artifact_inputs,
                    "protected_files": protected,
                }
            ),
            "artifacts_digest": recipe.digest,
            "operation_lock": captured["operation_lock"],
            "request_ids": scanned.request_ids,
            "attempt_ids": scanned.attempt_ids,
            "containment": scanned.containment.model_dump(mode="json"),
        }
        if artifacts(ctx, old, candidate) != (recipe, artifact_inputs):
            raise StepFailedError("host_renewal_state_changed")
        if installation(ctx, repo, sha, operation) != captured:
            raise StepFailedError("host_renewal_state_changed")
        with policy_preview.connection(path):
            if (
                store_fingerprint.capture_file(path) != before
                or protected_files(ctx) != protected
                or read_policy(ctx.review_policy) != raw
            ):
                raise StepFailedError("host_renewal_state_changed")
        return HostPlan.model_validate_json(
            canonical_json_value(fields | {"digest": _digest(fields)})
        )
    except StepFailedError:
        raise
    except (OSError, ValueError, sqlite3.Error):
        raise StepFailedError("host_renewal_state_changed") from None


def summary(plan: HostPlan) -> dict[str, object]:
    """Expose safe selectors, IDs, counts, digests and fixed codes."""
    return {
        "kind": plan.kind,
        "home": plan.home,
        "repo": plan.repo,
        "sha": plan.sha,
        "digest": plan.digest,
        "old_digest": plan.old["digest"],
        "candidate_digest": plan.candidate["digest"],
        "request_ids": plan.request_ids,
        "attempt_ids": plan.attempt_ids,
        "requests": len(plan.request_ids),
        "attempts": len(plan.attempt_ids),
        "unfinished_attempts": len(plan.containment.jobs),
        "policy_change": "host_only",
        "artifacts_digest": plan.artifacts_digest,
    }


def capture_bindings(
    ctx: Ctx, plan: HostPlan, operation: HeldOperation | None = None
) -> BindingInputs:
    """Capture exact approved bindings outside database writers."""
    old = parse_policy(plan.old["raw"])
    candidate = parse_policy(plan.candidate["raw"])
    if candidate.experimental_codex is None:
        raise StepFailedError("host_renewal_delta")
    check_host(candidate.experimental_codex, ctx.data)
    recipe, inputs = artifacts(ctx, old, candidate)
    if recipe.digest != plan.artifacts_digest:
        raise StepFailedError("host_renewal_plan_changed")
    return BindingInputs.model_validate_json(
        canonical_json_value(
            {
                "installation": installation(
                    ctx, Path(plan.repo), plan.sha, operation
                ),
                "artifacts": inputs,
                "protected_files": protected_files(ctx),
            }
        )
    )
