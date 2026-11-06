"""Resume an approved interface operation without rollback of its grant."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from install import (
    codex_interface,
    codex_interface_checks,
    legacy_state,
    lifecycle,
    policy_records,
    record,
)
from install.codex_interface_preview import summary
from install.context import Ctx, StepFailedError
from install.legacy_state import OperationCheck
from install.record import Record
from muninn import review_pause as q
from muninn import review_runtime_authority, store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest


def pending(ctx: Ctx) -> tuple[Ctx, Record] | None:
    """Find at most one bounded private unfinished typed operation receipt."""
    found: tuple[Ctx, Record] | None = None
    if not ctx.rdir.parent.is_dir():
        return None
    seen = 0
    with os.scandir(ctx.rdir.parent) as entries:
        for entry in entries:
            if not entry.name.startswith("muninn-install-"):
                continue
            seen += 1
            if seen > 128:
                raise StepFailedError("codex_interface_limit")
            directory = Path(entry.path)
            path = directory / "rollback-record.json"
            if not path.exists():
                continue
            rec = legacy_state.read(path)
            if "codex_interface" not in rec:
                continue
            value = policy_records.codex_interface(rec["codex_interface"])
            active = dataclasses.replace(ctx, ts=value.operation)
            if (
                directory != active.rdir
                or value.home != ctx.home
                or rec.get("ts") != value.operation
                or rec.get("repo") != str(value.repo)
                or rec.get("sha") != value.sha
                or not rec.get("upgrade")
                or rec.get("fresh")
            ):
                raise StepFailedError("codex_interface_record_changed")
            if (
                value.phase == "complete"
                and rec.get("install_complete") is True
            ):
                continue
            if found is not None:
                raise StepFailedError("codex_interface_recovery_required")
            found = active, rec
    return found


def resume(ctx: Ctx, rec: Record, check: OperationCheck) -> None:
    """Bind this exact checked lock acquisition to the retained operation."""
    value = policy_records.codex_interface(rec["codex_interface"])
    if (
        ctx.codex_interface != value.plan.digest
        or tuple(check()) != value.operation_identity
        or ctx.review_policy is not None
        or ctx.enable_learning
        or ctx.learning_mode is not None
        or not ctx.upgrade
        or ctx.fresh
    ):
        raise StepFailedError("codex_interface_recovery_required")
    ctx.interface_plan, ctx.interface_operation = value.plan, check
    lifecycle.stop(ctx)
    if value.phase in {"grant_committed", "complete"}:
        codex_interface_checks.before_start(ctx, rec)


def verify_committed(ctx: Ctx, rec: Record, conn: sqlite3.Connection) -> None:
    """Check grant facts while allowing normal generation/progress changes."""
    value = codex_interface.receipt(ctx, rec)
    if value.phase not in {"grant_committed", "complete"}:
        raise StepFailedError("codex_interface_recovery_required")
    codex_interface_checks.before_start(ctx, rec)
    actual = conn.execute("SELECT * FROM review_control").fetchone()
    if (
        actual is None
        or actual["policy_digest"] != digest(value.plan.candidate)
        or actual["policy_json"] != canonical_json(value.plan.candidate)
        or [
            store.db_path(ctx.data).stat().st_dev,
            store.db_path(ctx.data).stat().st_ino,
        ]
        != value.plan.state.store_identity
    ):
        raise StepFailedError("codex_interface_state_changed")
    codex_interface.verify_artifacts(ctx, value)
    capability = review_runtime_authority.selected_capability(
        ctx.data, digest(value.plan.candidate)
    )
    binding, approval = review_runtime_authority.checked_binding(
        ctx.data, capability
    )
    if (
        binding.requested != value.plan.candidate.runner
        or approval.plan_digest != value.plan.digest
    ):
        raise StepFailedError("codex_interface_record_changed")


def pause_failed(ctx: Ctx, rec: Record) -> None:
    """Retain the committed grant and owned pause after failure."""
    value = codex_interface.receipt(ctx, rec)
    if value.phase not in {"commit_pending", "grant_committed", "complete"}:
        return
    lifecycle.stop(ctx)
    rec["recovery_required"] = True
    with (
        q.maintenance_owner(
            ctx.data, wait_s=5.0, status=q.EraseStatus()
        ) as owner,
        q.maintenance_writer(owner),
        closing(store.connect_rw(store.db_path(ctx.data))) as conn,
    ):
        q.verify_current(owner, conn)
        if value.phase == "commit_pending":
            if codex_interface.recognize(conn, value) != "successor":
                record.save(ctx, rec)
                return
            codex_interface.verify_artifacts(ctx, value)
            previous = rec["codex_interface"]
            rec["codex_interface"] = value.model_copy(
                update={"phase": "grant_committed"}
            ).model_dump(mode="json")
            try:
                record.save(ctx, rec)
            except BaseException:
                rec["codex_interface"] = previous
                raise
        current = conn.execute(
            "SELECT policy_json,policy_digest FROM review_control"
        ).fetchone()
        if (
            current["policy_digest"] == digest(value.plan.candidate)
            and ReviewPolicy.model_validate_json(current["policy_json"])
            == value.plan.candidate
        ):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE review_control SET admission=0,generation=generation+1"
            )
            conn.execute("COMMIT")
    record.save(ctx, rec)


def install_owned(
    ctx: Ctx,
    repo: Path,
    sha: str,
    engine: Callable[[Ctx, Record, OperationCheck, bool], Record],
    prepare: Callable[[Ctx, Path, str], Record],
) -> Record:
    """Use the existing engine for an new or retained interface operation."""
    if ctx.dry_run:
        plan = codex_interface.preview(ctx)
        safe = summary(plan)
        ctx.say(json.dumps(safe))
        return {"codex_interface_preview": safe}
    found = pending(ctx)
    with legacy_state.operation(ctx) as check:
        if found is not None:
            active, rec = found
            if (rec["repo"], rec["sha"]) != (str(repo), sha):
                raise StepFailedError("codex_interface_recovery_required")
            with codex_interface.owned(active, check):
                resume(active, rec, check)
                return engine(active, rec, check, True)
        plan = ctx.interface_plan
        if plan is None:
            raise StepFailedError("codex_interface_approval_required")
        with codex_interface.owned(ctx, check):
            return engine(ctx, prepare(ctx, repo, sha), check, False)


def dispatch(
    ctx: Ctx,
    repo: Path,
    sha: str,
    interface: Callable[[Ctx, Path, str], Record],
    renewal: Callable[[Ctx, Path, str], Record],
) -> Record | None:
    """Dispatch rule changes while excluding unrelated operations."""
    if ctx.codex_interface is not None:
        return interface(ctx, repo, sha)
    if pending(ctx) is not None:
        raise StepFailedError("codex_interface_recovery_required")
    return (
        renewal(ctx, repo, sha) if ctx.renew_codex_host is not None else None
    )
