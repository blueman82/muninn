"""Validate saved postgrant authority without freezing later review work."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from install import legacy_baseline as baseline
from install import policy_records, review_policy
from install.context import Ctx, StepFailedError
from install.host_renewal_binding_models import BindingInputs
from install.host_renewal_bindings import check_transformed, verify_captured
from install.host_renewal_models import PendingFacts
from install.host_renewal_operation import validate_operation
from install.host_renewal_preview import capture_bindings
from install.policy_record_models import FileState
from install.record import Record
from muninn import review_runner, store
from muninn.review_models import ReviewPolicy
from tools.codex_policy_artifacts import plan_artifacts


@dataclass(frozen=True)
class ResumeInputs:
    """Retain a checked observation for the caller's later guarded recheck."""

    bindings: BindingInputs
    artifacts: dict[str, FileState]


def capture(ctx: Ctx, rec: Record, receipt: PendingFacts) -> ResumeInputs:
    """Check native signatures and saved transformations outside a writer."""
    try:
        current = capture_bindings(ctx, receipt.plan, ctx.host_operation)
        check_transformed(ctx, receipt.bindings, current)
        if policy_records.ready(rec.get("policy_ready"), ctx) != receipt.ready:
            raise StepFailedError("host_renewal_record_changed")
        review_policy.verify_ready(ctx, rec)
        if (
            baseline.file_state(Path(receipt.archive_path), private=True)
            != receipt.archive
        ):
            raise StepFailedError("host_renewal_record_changed")
        return ResumeInputs(current, candidate_files(ctx, receipt))
    except ValidationError:
        raise StepFailedError("host_renewal_state_changed") from None
    except ValueError as error:
        if str(error) not in {
            "capability_changed",
            "capability_storage",
            "capability_path",
            "capability_platform",
            "capability_executable_storage",
            "capability_executable_provenance",
            "codex_host_changed",
            "codex_platform_changed",
            "codex_data_home_changed",
            "preparation_artifact_invalid",
            "preparation_artifact_changed",
        }:
            raise
        raise StepFailedError("host_renewal_state_changed") from None


def candidate_files(ctx: Ctx, receipt: PendingFacts) -> dict[str, FileState]:
    """Require the original exact recipe from saved approved binding inputs."""
    policy = ReviewPolicy.model_validate_json(receipt.plan.candidate["raw"])
    saved = receipt.bindings.artifacts
    prompt = review_runner.read_private_file(
        ctx.data / "review-prompts" / (policy.prompt_digest + ".json"),
        policy.max_input_bytes,
    )
    recipe = plan_artifacts(
        policy, prompt, saved["binding"], saved["config_digest"]
    )
    if recipe.digest != receipt.plan.artifacts_digest:
        raise StepFailedError("host_renewal_record_changed")
    observed: dict[str, FileState] = {}
    for item in recipe.files:
        path = ctx.data / item.relative_path
        before = baseline.file_state(path, private=True)
        if (
            review_runner.read_private_file(path, 1024 * 1024) != item.raw
            or baseline.file_state(path, private=True) != before
        ):
            raise StepFailedError("host_renewal_state_changed")
        observed[str(path)] = TypeAdapter(FileState).validate_python(before)
    return observed


def guarded(
    ctx: Ctx,
    rec: Record,
    receipt: PendingFacts,
    inputs: ResumeInputs,
    path: Path,
    conn: sqlite3.Connection,
) -> None:
    """Recheck only immutable authority on the caller's fresh guarded image."""
    operation = ctx.host_operation
    if operation is None:
        raise StepFailedError("host_renewal_approval_required")
    validate_operation(ctx, operation)
    store.assert_store_format(conn)
    info = path.lstat()
    if [info.st_dev, info.st_ino] != receipt.contained.store_identity or dict(
        conn.execute("SELECT * FROM review_control").fetchone()
    ) != receipt.successor:
        raise StepFailedError("host_renewal_state_changed")
    try:
        verify_captured(ctx, receipt.plan, inputs.bindings)
    except ValueError as error:
        if str(error) not in {
            "codex_host_changed",
            "codex_platform_changed",
            "codex_data_home_changed",
        }:
            raise
        raise StepFailedError("host_renewal_state_changed") from None
    review_policy.verify_ready(ctx, rec)
    for name, expected in inputs.artifacts.items():
        if baseline.file_state(Path(name), private=True) != expected:
            raise StepFailedError("host_renewal_state_changed")
    validate_operation(ctx, operation)
