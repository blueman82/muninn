"""Publish a verified install and retire superseded recovery records."""

from __future__ import annotations

import shutil

from pydantic import TypeAdapter, ValidationError

from install import (
    host_renewal_receipts,
    legacy_state,
    policy_records,
    review_policy,
)
from install.constants import INSTALL_RECORD
from install.context import Ctx, StepFailedError
from install.host_renewal_bindings import capture_prior_summary
from install.policy_record_models import FileState
from install.record import Record, install_record, install_summary, save
from install.snapshot import discard
from install.steps_release import sweep
from muninn.review_json import canonical_json_value


def supersede(ctx: Ctx, sha: str) -> None:
    """Retire a completed activation record after a replacement verifies."""
    found = review_policy.pending(ctx)
    if found is None:
        return
    active, rec = found
    if (
        rec["sha"] == sha
        or rec.get("policy_status") != "active"
        or policy_records.activation_phase(rec) != "complete"
        or rec.get("failed", {}).get("step") != "verify"
    ):
        return
    rec["superseded_by"] = sha
    rec.pop("recovery_required", None)
    save(active, rec)


def complete(ctx: Ctx, rec: Record) -> Record:
    """Retain recovery authority and publish the verified final outcome."""
    policy_records.validate(rec)
    if "host_renewal" in rec and publication_complete(ctx, rec):
        return rec
    renewal = (
        policy_records.host_renewal(rec["host_renewal"])
        if "host_renewal" in rec
        else None
    )
    if renewal is None or renewal.phase != "upgrade_verified":
        # The verified receipt retains final metadata for publication retries.
        if ctx.upgrade and not ctx.dry_run:
            supersede(ctx, rec["sha"])
        rec["sweep_failed"] = sweep(ctx)
        discard(ctx, rec)
        if renewal is not None:
            host_renewal_receipts.verified(ctx, rec)
    if (
        ctx.upgrade
        and not ctx.dry_run
        and policy_records.activation_phase(rec) is None
        and "host_renewal" not in rec
    ):
        shutil.rmtree(ctx.rdir)
        rec["record_removed"] = True
    rec["install_complete"] = True
    if "host_renewal" in rec:
        policy_records.check_renewal_size(rec)
        install_record(ctx, rec, "ok")
        save(ctx, rec)
    else:
        if not ctx.dry_run and not rec.get("record_removed"):
            save(ctx, rec)
        install_record(ctx, rec, "ok")
    if ctx.dry_run:
        ctx.say("dry run only: nothing was written")
    else:
        where = (
            ctx.lib / INSTALL_RECORD if rec.get("record_removed") else ctx.rdir
        )
        ctx.say(f"install done; record in {where}")
    return rec


def finalize(ctx: Ctx, rec: Record) -> Record:
    """Publish actual verification and preserve committed failure truth."""
    try:
        return complete(ctx, rec)
    except Exception:
        if "host_renewal" in rec:
            ctx.say("host approval committed; upgrade incomplete")
        raise


def publication_complete(ctx: Ctx, rec: Record) -> bool:
    """Require exact prior or same-operation provenance before publication."""
    before, current = _summary(ctx)
    if before is None:
        return False
    expected = install_summary(ctx, rec, "ok")
    if _same_projection(current, expected):
        return rec.get("install_complete") is True
    receipt = policy_records.host_renewal(rec["host_renewal"])
    if before not in (receipt.prior_summary, receipt.recognized_summary):
        raise StepFailedError("host_renewal_record_changed")
    return False


def recognize_failure_summary(ctx: Ctx, rec: Record) -> FileState | None:
    """Check exact durable failure status before outer metadata can advance."""
    receipt = policy_records.host_renewal(rec["host_renewal"])
    before, current = _summary(ctx)
    if before is None or before in (
        receipt.prior_summary,
        receipt.recognized_summary,
    ):
        return receipt.recognized_summary
    if receipt.phase == "upgrade_verified" and _same_projection(
        current, install_summary(ctx, rec, "ok")
    ):
        return receipt.recognized_summary
    expected = install_summary(ctx, rec, "recovery_required")
    if not _same_projection(current, expected) or not _stale_failure(
        current, expected | {"steps": list(ctx.host_final_steps)}
    ):
        raise StepFailedError("host_renewal_record_changed")
    return before


def _summary(ctx: Ctx) -> tuple[FileState | None, Record]:
    """Read one bounded private summary with a stable file fingerprint."""
    before = capture_prior_summary(ctx)
    if before is None:
        return None, {}
    current = legacy_state.read(ctx.lib / INSTALL_RECORD)
    if capture_prior_summary(ctx) != before:
        raise StepFailedError("host_renewal_record_changed")
    return before, current


def _stale_failure(current: Record, expected: Record) -> bool:
    """Accept only the finite earlier failure projection of this operation."""
    mutable = {
        "steps",
        "failed",
        "sweep_failed",
        "recovery_required",
        "host_renewal_phase",
        "host_approval_committed",
        "outcome",
    }
    if set(current) != set(expected) or not _same_projection(
        {key: current[key] for key in expected.keys() - mutable},
        {key: expected[key] for key in expected.keys() - mutable},
    ):
        return False
    phase = current["host_renewal_phase"]
    if type(phase) is not str or phase not in {
        "approved_intent",
        "contained_archived",
        "commit_pending",
        "grant_committed",
        "restart_pending",
    }:
        return False
    committed = phase in {"grant_committed", "restart_pending"}
    if (
        current["host_approval_committed"] is not committed
        or current["outcome"] != "recovery_required"
        or current["recovery_required"] is not True
    ):
        return False
    try:
        steps = TypeAdapter(list[str]).validate_python(
            current["steps"], strict=True
        )
        failed = TypeAdapter(dict[str, str]).validate_python(
            current["failed"], strict=True
        )
    except ValidationError:
        raise StepFailedError("host_renewal_record_changed") from None
    if steps != expected["steps"][: len(steps)] or set(failed) != {
        "step",
        "error",
    }:
        return False
    if failed["step"] not in expected["steps"] or failed["error"] not in {
        "StepFailedError",
        "OSError",
        "OperationalError",
        "DatabaseError",
        "IntegrityError",
        "RefusedError",
        "RacedError",
        "ValueError",
        "ValidationError",
    }:
        return False
    return (
        current["sweep_failed"] is None
        or current["sweep_failed"] == expected["sweep_failed"]
    )


def _same_projection(left: Record, right: Record) -> bool:
    """Compare JSON values without coercing booleans or numeric field types."""
    try:
        return canonical_json_value(left) == canonical_json_value(right)
    except ValueError:
        raise StepFailedError("host_renewal_record_changed") from None
