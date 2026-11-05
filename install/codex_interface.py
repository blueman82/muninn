"""Apply one approved stable Codex interface under installer ownership."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path

from install import (
    codex_interface_recovery,
    policy_records,
    record,
    snapshot_io,
)
from install.codex_interface_models import (
    InterfaceApproval,
    InterfacePlan,
    InterfaceReceipt,
)
from install.codex_interface_preview import legacy_binding, preview
from install.context import Ctx, StepFailedError
from install.host_renewal_preview import scan_state
from install.record import Record
from muninn import review_managed_files, store
from muninn import review_pause as q
from muninn.review_json import canonical_json
from muninn.review_progress import digest
from muninn.review_runner import read_private_file
from tools.codex_interface_artifacts import plan_artifacts
from tools.codex_policy_artifacts import publish_files

_ACTIVE: ContextVar[Callable[[], tuple[int, int]] | None] = ContextVar(
    "codex_interface_operation",
    default=None,
)


@contextmanager
def owned(ctx: Ctx, check: Callable[[], tuple[int, int]]) -> Generator[None]:
    """Bind interface mutation to this live normal installer acquisition."""
    if _ACTIVE.get() is not None:
        raise StepFailedError("codex_interface_approval_required")
    check()
    token = _ACTIVE.set(check)
    ctx.interface_operation = check
    try:
        yield
        check()
    finally:
        ctx.interface_operation = None
        _ACTIVE.reset(token)


def confirm(
    plan: InterfacePlan, prompt: Callable[[str], str] | None, now: float
) -> InterfaceApproval:
    """Obtain fresh yes/y consent for the disclosed initial UPDATE RULE."""
    if prompt is None:
        if not sys.stdin.isatty():
            raise StepFailedError("codex_interface_approval_required")
        prompt = input
    disclosure = (
        f"Approve UPDATE RULE {plan.digest}?\n"
        "Trust this computer's fixed local Codex manager and checked JSON rev"
        "iew interface. "
        "Compatible future updates keep this same grant; version/hash are per"
        "-call facts.\n"
        "The main executable is checked; local package metadata is not publis"
        "her proof. "
        "Same-user compromise and auxiliary package origin remain unverified."
        "\n"
        "Keep the exact account, auth home, model/reasoning, projects, permis"
        "sions, history, "
        "attempts, charges and limits. No new API or billing route.\n"
        f"Preserve {len(plan.bridge.requests)} eligible old requests. "
        "Containment does not reset consumed attempts or uncertain charges.\n"
        "The ordinary upgrade can restart admitted learning. Provider billing"
        " and full isolation "
        "remain unqualified. A committed new grant stays paused on later fail"
        "ure; "
        "an old runtime cannot safely roll it back.\n"
        "Approve this exact rule now? [y/N] "
    )
    try:
        answer = prompt(disclosure)
    except (OSError, EOFError):
        raise StepFailedError("codex_interface_approval_required") from None
    if answer.strip().casefold() not in {"y", "yes"}:
        raise StepFailedError("codex_interface_approval_required")
    return InterfaceApproval(
        plan_digest=plan.digest,
        approved_at=now,
        authority="installer_prompt_yes",
    )


def receipt(ctx: Ctx, rec: Record) -> InterfaceReceipt:
    """Require the exact durable operation and its held installer lock."""
    value = policy_records.codex_interface(rec.get("codex_interface"))
    check = ctx.interface_operation
    if (
        check is None
        or _ACTIVE.get() is not check
        or tuple(check()) != value.operation_identity
    ):
        raise StepFailedError("codex_interface_approval_required")
    if (
        value.operation != ctx.ts
        or value.home != ctx.home
        or str(value.repo) != rec.get("repo")
        or value.sha != rec.get("sha")
        or value.approval.plan_digest != value.plan.digest
        or ctx.codex_interface != value.plan.digest
    ):
        raise StepFailedError("codex_interface_record_changed")
    return value


def phase(
    ctx: Ctx, rec: Record, name: str, **changes: object
) -> InterfaceReceipt:
    """Persist one validated complete phase before the next side effect."""
    value = receipt(ctx, rec)
    updated = value.model_copy(update={"phase": name, **changes})
    checked = InterfaceReceipt.model_validate_json(canonical_json(updated))
    rec["codex_interface"] = checked.model_dump(mode="json")
    record.save(ctx, rec)
    return checked


def run(ctx: Ctx, rec: Record, plan: InterfacePlan) -> None:
    """Record fresh consent under the normal installer operation owner."""
    if (
        ctx.dry_run
        or ctx.interface_operation is None
        or _ACTIVE.get() is not ctx.interface_operation
        or ctx.codex_interface != plan.digest
        or ctx.interface_plan != plan
    ):
        raise StepFailedError("codex_interface_approval_required")
    if preview(ctx) != plan:
        raise StepFailedError("codex_interface_plan_changed")
    approval = confirm(plan, ctx.learning_prompt, float(ctx.now()))
    bundle = plan_artifacts(plan, approval, ctx.ts)
    value = InterfaceReceipt(
        kind="codex-managed-interface-receipt-v1",
        phase="approved_intent",
        operation=ctx.ts,
        home=ctx.home,
        repo=Path(rec["repo"]),
        sha=rec["sha"],
        plan=plan,
        approval=approval,
        operation_identity=ctx.interface_operation(),
        predecessor=None,
        successor=None,
        contained=None,
        artifacts_digest=bundle.digest,
        minimum_runtime="codex-managed-interface-v1",
    )
    rec["codex_interface"] = value.model_dump(mode="json")
    record.save(ctx, rec)


def before_owner(
    ctx: Ctx, rec: Record, owner: q.MaintenanceOwner, path: Path
) -> bool:
    """Recheck immutable state after verified predecessor containment."""
    value = receipt(ctx, rec)
    if value.phase != "approved_intent":
        return False
    with (
        q.maintenance_writer(owner),
        snapshot_io.validated(path) as check,
        closing(store.connect_rw(path)) as conn,
    ):
        q.verify_current(owner, conn)
        scanned = scan_state(
            conn,
            path,
            candidate_digest=digest(value.plan.candidate),
            containment=value.plan.containment,
            check=ctx.interface_operation,
        )
        if (
            scanned.containment.invariant_digest
            != value.plan.containment.invariant_digest
        ):
            raise StepFailedError("codex_interface_state_changed")
        check()
        current = scanned.state.control
    if value.owner_control is None:
        phase(ctx, rec, "approved_intent", owner_control=current)
        return True
    if current == value.owner_control:
        return True
    if current == value.owner_control | {
        "generation": value.owner_control["generation"] + 1
    }:
        return False
    raise StepFailedError("codex_interface_state_changed")


def contained(
    ctx: Ctx, rec: Record, owner: q.MaintenanceOwner, path: Path
) -> None:
    """Save the contained predecessor without resetting queue or charges."""
    value = receipt(ctx, rec)
    if value.phase != "approved_intent":
        return
    with q.maintenance_writer(owner), snapshot_io.validated(path) as check:
        with closing(store.connect_rw(path)) as conn:
            q.verify_current(owner, conn)
            scanned = scan_state(
                conn,
                path,
                candidate_digest=digest(value.plan.candidate),
                containment=value.plan.containment,
                check=ctx.interface_operation,
            )
            if (
                scanned.containment.invariant_digest
                != value.plan.containment.invariant_digest
            ):
                raise StepFailedError("codex_interface_state_changed")
            if (
                value.owner_control is None
                or scanned.state.control
                != value.owner_control
                | {
                    "generation": value.owner_control["generation"] + 1,
                }
            ):
                raise StepFailedError("codex_interface_state_changed")
            check()
        phase(ctx, rec, "contained", contained=scanned.state)


def verify_artifacts(ctx: Ctx, value: InterfaceReceipt) -> None:
    """Require the approved authority bytes without reconstructing consent."""
    bundle = plan_artifacts(value.plan, value.approval, value.operation)
    if bundle.digest != value.artifacts_digest:
        raise StepFailedError("codex_interface_record_changed")
    for item in bundle.files:
        if (
            read_private_file(ctx.data / item.relative_path, 256 * 1024)
            != item.raw
        ):
            raise StepFailedError("codex_interface_record_changed")


def _runtime(ctx: Ctx, value: InterfaceReceipt) -> None:
    """Require compatible code before publishing the new grant."""
    selected = (ctx.lib / "current").resolve(strict=True)
    if selected != ctx.lib / value.sha:
        raise StepFailedError("codex_interface_runtime_required")
    marker = selected / "muninn/review_runtime_models.py"
    if (
        not marker.is_file()
        or b"codex_managed_interface_v1" not in marker.read_bytes()
    ):
        raise StepFailedError("codex_interface_runtime_required")


def activate(
    ctx: Ctx, rec: Record, owner: q.MaintenanceOwner, path: Path
) -> None:
    """Publish inert artifacts, retain commit intent, then change control."""
    value = receipt(ctx, rec)
    if value.phase in {"grant_committed", "complete"}:
        return
    if value.contained is None:
        raise StepFailedError("codex_interface_recovery_required")
    _runtime(ctx, value)
    if not review_managed_files.matches(value.plan.observation):
        raise StepFailedError("codex_interface_plan_changed")
    if legacy_binding(ctx, value.plan.old) != value.plan.old_binding:
        raise StepFailedError("codex_interface_state_changed")
    bundle = plan_artifacts(value.plan, value.approval, value.operation)
    publish_files(ctx.data, bundle.files)
    verify_artifacts(ctx, value)
    with q.maintenance_writer(owner), snapshot_io.validated(path) as check:
        with closing(store.connect_rw(path)) as conn:
            q.verify_current(owner, conn)
            current = dict(
                conn.execute("SELECT * FROM review_control").fetchone()
            )
            if (
                value.phase == "commit_pending"
                and recognize(conn, value) == "successor"
            ):
                phase(ctx, rec, "grant_committed")
                return
            if current != value.contained.control:
                raise StepFailedError("codex_interface_state_changed")
            predecessor = value.contained.control
            successor = predecessor | {
                "policy_json": canonical_json(value.plan.candidate),
                "policy_digest": digest(value.plan.candidate),
                "generation": predecessor["generation"] + 1,
            }
            value = phase(
                ctx,
                rec,
                "commit_pending",
                predecessor=predecessor,
                successor=successor,
            )
            check()
            commit_transaction(ctx, rec, conn, value)
        phase(ctx, rec, "grant_committed")
        check()


def commit_transaction(
    ctx: Ctx, rec: Record, conn: sqlite3.Connection, value: InterfaceReceipt
) -> None:
    """Recheck the predecessor in the same immediate SQL transaction."""
    if (
        value.phase != "commit_pending"
        or value.predecessor is None
        or value.successor is None
    ):
        raise StepFailedError("codex_interface_recovery_required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        receipt(ctx, rec)
        scanned = scan_state(
            conn,
            store.db_path(ctx.data),
            candidate_digest=digest(value.plan.candidate),
            containment=value.plan.containment,
            check=ctx.interface_operation,
        )
        if (
            scanned.state != value.contained
            or dict(conn.execute("SELECT * FROM review_control").fetchone())
            != value.predecessor
        ):
            raise StepFailedError("codex_interface_state_changed")
        verify_artifacts(ctx, value)
        if not review_managed_files.matches(value.plan.observation):
            raise StepFailedError("codex_interface_plan_changed")
        conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?,generati"
            "on=? WHERE singleton=1",
            (
                value.successor["policy_json"],
                value.successor["policy_digest"],
                value.successor["generation"],
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        with suppress(BaseException):
            conn.execute("ROLLBACK")
        raise


def recognize(conn: sqlite3.Connection, value: InterfaceReceipt) -> str:
    """Recognize only the complete exact precommit predecessor or successor."""
    if (
        value.phase != "commit_pending"
        or value.predecessor is None
        or value.successor is None
    ):
        raise StepFailedError("codex_interface_recovery_required")
    current = dict(conn.execute("SELECT * FROM review_control").fetchone())
    scanned = scan_state(
        conn,
        Path(conn.execute("PRAGMA database_list").fetchone()[2]),
        candidate_digest=digest(value.plan.candidate),
        containment=value.plan.containment,
        check=None,
    )
    expected = value.contained
    if (
        expected is not None
        and current == value.predecessor
        and scanned.state == expected
    ):
        return "predecessor"
    if (
        expected is not None
        and current == value.successor
        and scanned.state
        == expected.model_copy(update={"control": value.successor})
    ):
        return "successor"
    raise StepFailedError("codex_interface_state_changed")


def prepare(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    current: Callable[[Ctx, Record, q.MaintenanceOwner], Path],
) -> Path:
    """Contain the current store before the one-time policy commit."""
    value = receipt(ctx, rec)
    path = store.db_path(ctx.data)
    if value.phase == "approved_intent":
        if before_owner(ctx, rec, owner, path):
            path = current(ctx, rec, owner)
        contained(ctx, rec, owner, path)
    elif value.phase in {"grant_committed", "complete"}:
        with (
            q.maintenance_writer(owner),
            snapshot_io.validated(path) as checked,
            closing(store.connect_rw(path)) as conn,
        ):
            q.verify_current(owner, conn)
            codex_interface_recovery.verify_committed(ctx, rec, conn)
            checked()
    return path
