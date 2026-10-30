"""Commit one explicitly approved host-only policy change under ownership."""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing, suppress
from pathlib import Path

from install import (
    host_renewal_resume,
    policy_records,
    record,
    review_policy,
    snapshot_io,
)
from install import legacy_baseline as baseline
from install.context import Ctx, StepFailedError
from install.host_renewal_bindings import (
    capture_prior_summary,
    capture_setup_notice,
    check_transformed,
    verify_captured,
)
from install.host_renewal_models import (
    ContainedFacts,
    HostApproval,
    HostPlan,
    PendingFacts,
    RenewalReceipt,
)
from install.host_renewal_operation import validate_operation
from install.host_renewal_preview import (
    StateScan,
    artifacts,
    capture_bindings,
    preview,
    scan_state,
)
from install.host_renewal_receipts import (
    check_future_records,
)
from install.host_renewal_receipts import (
    phase as phase,
)
from install.host_renewal_receipts import (
    recognize as recognize,
)
from install.legacy_state import OperationCheck
from install.record import Record
from muninn import (
    file_sync,
    platform_io,
    review_pause_owner,
    store,
    tombstones,
)
from muninn import review_pause as q
from muninn.review_models import ReviewPolicy
from tools.codex_policy_artifacts import publish_artifacts


def confirm(
    plan: HostPlan, prompt: Callable[[str], str], now: float
) -> HostApproval:
    """Obtain a fresh affirmative answer for the fully disclosed exact plan."""
    requests = tuple(job.request.id for job in plan.containment.jobs)
    attempts = tuple(job.attempt.id for job in plan.containment.jobs)
    disclosure = (
        f"Approve this computer-only policy change for plan {plan.digest}?\n"
        "Model, account, authentication home, projects and policy "
        "limits stay unchanged.\n"
        f"Containment covers {len(attempts)} unfinished no-call attempts. "
        "Unproved containment refuses; actual records and pauses "
        "stay retained.\n"
        f"Affected requests: {requests}.\n"
        f"Affected attempts: {attempts}.\n"
        "Old work is not replayed. History review needs separate "
        "explicit authority.\n"
        "This is ongoing permission with no aggregate call or "
        "spending cap of its own. "
        "Existing per-call, attempt, round and concurrency limits remain.\n"
        "The full upgrade starts Codex app-server initialize and "
        "hooks/list checks. "
        "Background learning may start before the upgrade returns. "
        "Provider costs and full isolation are not qualified.\n"
        "A committed host approval survives a later upgrade failure. "
        "A later identity change can require another renewal.\n"
        "Approve this exact plan now? [y/N] "
    )
    try:
        answer = prompt(disclosure)
    except (EOFError, OSError):
        raise StepFailedError("host_renewal_approval_required") from None
    if answer.strip().casefold() not in {"yes", "y"}:
        raise StepFailedError("host_renewal_approval_required")
    return HostApproval(
        plan_digest=plan.digest,
        approved_at=now,
        authority="installer_prompt",
        confirmed=True,
    )


def authority(ctx: Ctx, rec: Record, check: OperationCheck) -> RenewalReceipt:
    """Check exact receipt selectors and the live operation observation."""
    value = policy_records.host_renewal(rec.get("host_renewal"))
    operation = ctx.host_operation
    if operation is None or operation.check is not check:
        raise StepFailedError("host_renewal_approval_required")
    validate_operation(ctx, operation)
    if (
        (value.operation, value.home, value.repo, value.sha)
        != (ctx.ts, str(ctx.home), rec["repo"], rec["sha"])
        or value.held_operation_lock != operation.held
        or value.operation_identity != list(check())
    ):
        raise StepFailedError("host_renewal_record_changed")
    return value


def stage(
    ctx: Ctx, rec: Record, plan: HostPlan, check: OperationCheck
) -> None:
    """Durably retain exact approved authority before any disruptive step."""
    operation, approval = ctx.host_operation, ctx.host_approval
    if (
        operation is None
        or approval is None
        or approval.plan_digest != plan.digest
        or ctx.renew_codex_host != plan.digest
    ):
        raise StepFailedError("host_renewal_approval_required")
    validate_operation(ctx, operation)
    if preview(ctx, Path(plan.repo), plan.sha, operation=operation) != plan:
        raise StepFailedError("host_renewal_plan_changed")
    bindings = capture_bindings(ctx, plan, operation)
    setup_notice = capture_setup_notice(ctx, plan)
    rec["host_renewal"] = {
        "version": 1,
        "phase": "approved_intent",
        "operation": ctx.ts,
        "home": plan.home,
        "repo": plan.repo,
        "sha": plan.sha,
        "plan": plan.model_dump(mode="json"),
        "approval": approval.model_dump(mode="json"),
        "operation_identity": list(check()),
        "held_operation_lock": operation.held,
        "bindings": bindings.model_dump(mode="json"),
        "prior_summary": capture_prior_summary(ctx),
        "recognized_summary": None,
        "setup_notice": (
            setup_notice.model_dump(mode="json") if setup_notice else None
        ),
        "containment_started_at": float(ctx.now()),
    }
    policy_records.validate(rec)
    authority(ctx, rec, check)
    record.save(ctx, rec)


def _scan(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    path: Path,
    check: OperationCheck,
    *,
    complete: bool,
) -> StateScan:
    """Check one freshly guarded exact no-call containment observation."""
    value = authority(ctx, rec, check)
    plan = value.plan
    review_pause_owner.same_owner(
        plan.containment.witness, review_pause_owner.latest(ctx.data)
    )
    with closing(store.connect_rw(path)) as conn:
        q.verify_current(owner, conn)
        tombstones.require_current_tombstones(conn, ctx.data)
        scanned = scan_state(
            conn,
            path,
            candidate_digest=plan.candidate["digest"],
            containment=plan.containment,
            check=check,
        )
    if (
        scanned.containment.invariant_digest
        != plan.containment.invariant_digest
    ):
        raise StepFailedError("host_renewal_state_changed")
    now = float(ctx.now())
    for job in scanned.containment.jobs:
        ended = job.attempt.ended_at
        if (complete and ended is None) or (
            ended is not None
            and not value.containment_started_at <= ended <= now
        ):
            raise StepFailedError("host_renewal_state_changed")
    return scanned


def after_owner(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    path: Path,
    check: OperationCheck,
) -> None:
    """Record the exact checked control before the normal current fence."""
    value = authority(ctx, rec, check)
    if value.phase != "approved_intent" or value.owner_control is not None:
        raise StepFailedError("host_renewal_recovery_required")
    with (
        q.maintenance_writer(owner),
        snapshot_io.validated(path) as revalidate,
    ):
        scanned = _scan(ctx, rec, owner, path, check, complete=False)
        revalidate()
        phase(ctx, rec, "approved_intent", owner_control=scanned.state.control)


def contained(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    path: Path,
    check: OperationCheck,
) -> None:
    """Retain a guarded raw archive only after exact no-call containment."""
    with (
        q.maintenance_writer(owner),
        snapshot_io.validated(path) as revalidate,
    ):
        scanned = _scan(ctx, rec, owner, path, check, complete=True)
        before = baseline.file_state(path, private=True)
        archive = ctx.rdir / "host-store-archive.sqlite"
        rec["host_archive"] = {"path": str(archive), "status": "partial"}
        record.save(ctx, rec)
        with platform_io.open_regular(path) as source:
            fd = platform_io.open_private(
                archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            )
            with os.fdopen(fd, "wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                file_sync.sync_fd(target.fileno())
        file_sync.sync_path(ctx.rdir)
        archived = baseline.file_state(archive, private=True)
        if (
            baseline.file_state(path, private=True) != before
            or archived["sha256"] != before["sha256"]
            or archived["stamp"][3] != before["stamp"][3]
        ):
            raise StepFailedError("host_renewal_state_changed")
        revalidate()
        phase(
            ctx,
            rec,
            "contained_archived",
            contained=scanned.state.model_dump(mode="json"),
            archive=archived,
            archive_path=str(archive),
            archive_format=2,
            archive_status="complete",
            containment_finished_at=float(ctx.now()),
        )
        rec["host_archive"]["status"] = "complete"
        record.save(ctx, rec)


def activate(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    path: Path,
    check: OperationCheck,
) -> None:
    """Prepare outside writers, then commit one exact policy-only grant."""
    value = authority(ctx, rec, check)
    if not isinstance(value, ContainedFacts) or value.phase not in {
        "contained_archived",
        "commit_pending",
        "grant_committed",
        "restart_pending",
    }:
        raise StepFailedError("host_renewal_recovery_required")
    if (
        baseline.file_state(Path(value.archive_path), private=True)
        != value.archive
    ):
        raise StepFailedError("host_renewal_record_changed")
    if isinstance(value, PendingFacts) and value.phase in {
        "grant_committed",
        "restart_pending",
    }:
        checked = host_renewal_resume.capture(ctx, rec, value)
        with (
            q.maintenance_writer(owner),
            snapshot_io.validated(path) as revalidate,
            closing(store.connect_rw(path)) as conn,
        ):
            q.verify_current(owner, conn)
            host_renewal_resume.guarded(ctx, rec, value, checked, path, conn)
            revalidate()
        return
    plan = value.plan
    current = capture_bindings(ctx, plan, ctx.host_operation)
    check_transformed(ctx, value.bindings, current)
    recipe, _ = artifacts(
        ctx,
        ReviewPolicy.model_validate_json(plan.old["raw"]),
        ReviewPolicy.model_validate_json(plan.candidate["raw"]),
    )
    try:
        publish_artifacts(ctx.data, recipe)
    except ValueError as error:
        if str(error) not in {
            "preparation_artifact_changed",
            "preparation_artifact_invalid",
        }:
            raise
        raise StepFailedError("host_renewal_state_changed") from None
    review_policy.ready(ctx, rec)
    review_policy.verify_ready(ctx, rec)
    with (
        q.maintenance_writer(owner),
        snapshot_io.validated(path) as revalidate,
    ):
        with closing(store.connect_rw(path)) as conn:
            q.verify_current(owner, conn)
            tombstones.reapply_tombstones(conn, ctx.data)
            tombstones.require_current_tombstones(conn, ctx.data)
            data_version = conn.execute("PRAGMA data_version").fetchone()[0]
            scanned = scan_state(
                conn,
                path,
                candidate_digest=plan.candidate["digest"],
                containment=plan.containment,
                check=check,
            )
            if value.phase == "commit_pending":
                outcome = recognize(conn, value, scanned.state)
                if outcome == "successor":
                    ctx.host_grant_observed = True
                    phase(ctx, rec, "grant_committed")
                    return
            elif scanned.state != value.contained:
                raise StepFailedError("host_renewal_state_changed")
            successor = value.contained.control | {
                "policy_json": plan.candidate["canonical"],
                "policy_digest": plan.candidate["digest"],
                "generation": value.contained.control["generation"] + 1,
            }
            pending = phase(
                ctx,
                rec,
                "commit_pending",
                predecessor=value.contained.control,
                successor=successor,
                ready=rec["policy_ready"],
            )
            check_future_records(rec, pending, ctx.host_final_steps)
            authority(ctx, rec, check)
            verify_captured(ctx, plan, current)
            review_policy.verify_ready(ctx, rec)
            revalidate()
            commit_policy_transaction(ctx, rec, conn, pending, data_version)
        phase(ctx, rec, "grant_committed")
        revalidate()


def commit_policy_transaction(
    ctx: Ctx,
    rec: Record,
    conn: sqlite3.Connection,
    pending: RenewalReceipt,
    data_version: int,
) -> None:
    """Commit exactly the checked pending policy under the caller's writer."""
    if not isinstance(pending, PendingFacts) or conn.in_transaction:
        raise StepFailedError("host_renewal_recovery_required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        actual = dict(conn.execute("SELECT * FROM review_control").fetchone())
        if (
            actual != pending.predecessor
            or conn.execute("PRAGMA data_version").fetchone()[0]
            != data_version
        ):
            raise StepFailedError("host_renewal_state_changed")
        conn.execute(
            "UPDATE review_control SET "
            "policy_json=?,policy_digest=?,generation=generation+1 "
            "WHERE singleton=1",
            (
                pending.plan.candidate["canonical"],
                pending.plan.candidate["digest"],
            ),
        )
        conn.execute("COMMIT")
        ctx.host_grant_observed = True
    except BaseException:
        if (
            not conn.in_transaction
            and dict(conn.execute("SELECT * FROM review_control").fetchone())
            == pending.successor
        ):
            ctx.host_grant_observed = True
            phase(ctx, rec, "grant_committed")
        else:
            with suppress(BaseException):
                conn.execute("ROLLBACK")
        raise
