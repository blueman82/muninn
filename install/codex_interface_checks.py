"""Check physical authority and selected parser files without execution."""

from __future__ import annotations

import ast
from pathlib import Path

from install import legacy_baseline as baseline
from install import policy_preview, policy_records
from install.codex_interface_legacy import legacy_facts
from install.codex_interface_models import InterfaceReceipt
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import platform_io, store
from muninn.review_codex_policy import ManagedCodexExperiment, check_host
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_runtime_models import MANAGED_HOST_RUNTIME

_SOURCES = (
    "muninn/review_runtime_models.py",
    "muninn/review_codex_policy.py",
    "muninn/review_models.py",
)


def managed_policy(ctx: Ctx, rec: Record) -> ReviewPolicy | None:
    """Read an exact selected policy without acquiring another writer."""
    if "codex_interface" in rec:
        return policy_records.codex_interface(
            rec["codex_interface"]
        ).plan.candidate
    raw = policy_records.input_raw(rec)
    if raw is not None:
        policy = ReviewPolicy.model_validate_json(raw)
    elif store.db_path(ctx.data).exists():
        with policy_preview.connection(store.db_path(ctx.data)) as conn:
            row = conn.execute(
                "SELECT policy_json,policy_digest FROM review_control"
            ).fetchone()
            if row is None or row[0] is None:
                return None
            policy = ReviewPolicy.model_validate_json(row[0])
            if digest(policy) != row[1]:
                raise StepFailedError("review_policy_state_invalid")
    else:
        return None
    return (
        policy
        if isinstance(policy.experimental_codex, ManagedCodexExperiment)
        else None
    )


def _source(path: Path, expected: object) -> ast.Module:
    """Parse bounded source only when its prepared file identity is exact."""
    if path.lstat().st_size > 256 * 1024:
        raise ValueError
    before = baseline.file_state(path)
    if before != expected:
        raise ValueError
    with platform_io.open_regular(path) as handle:
        raw = handle.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024 or baseline.file_state(path) != before:
        raise ValueError
    return ast.parse(raw)


def capture_runtime(ctx: Ctx, rec: Record, destination: Path) -> None:
    """Bind prepared source without requiring an already migrated store."""
    if (
        (ctx.platform == "darwin" and ctx.upgrade)
        or ctx.review_policy
        or policy_records.input_raw(rec)
    ):
        rec["prepared_runtime"] = baseline.tree_state(destination, links=True)


def runtime(ctx: Ctx, rec: Record) -> None:
    """Refuse old or changed parsers before exposing a managed grant."""
    try:
        selected = (ctx.lib / "current").resolve(strict=True)
        if selected != ctx.lib / rec["sha"] or selected.is_symlink():
            raise ValueError
        prepared = rec["prepared_runtime"]
        for name in (".", "muninn"):
            if (
                baseline.directory(selected / name)
                != prepared["directories"][name]
            ):
                raise ValueError
        models, terms, policy = (
            _source(selected / name, prepared["files"][name])
            for name in _SOURCES
        )
        marker = any(
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "MANAGED_HOST_RUNTIME"
            and isinstance(node.value, ast.Constant)
            and node.value.value == MANAGED_HOST_RUNTIME
            for node in models.body
        )
        parser = any(
            isinstance(node, ast.ClassDef)
            and node.name == "ManagedCodexExperiment"
            and any(
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id == "kind"
                and any(
                    isinstance(value, ast.Constant)
                    and value.value == "ongoing-mac-codex-managed-v1"
                    for value in ast.walk(item.annotation)
                )
                for item in node.body
            )
            for node in terms.body
        )
        integrated = any(
            isinstance(node, ast.ClassDef)
            and node.name == "ReviewPolicy"
            and any(
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id == "experimental_codex"
                and any(
                    isinstance(value, ast.Name)
                    and value.id == "ManagedCodexExperiment"
                    for value in ast.walk(item.annotation)
                )
                for item in node.body
            )
            for node in policy.body
        )
        if not marker or not parser or not integrated:
            raise ValueError
        if (ctx.lib / "current").resolve(strict=True) != selected:
            raise ValueError
        for name in (".", "muninn"):
            if (
                baseline.directory(selected / name)
                != prepared["directories"][name]
            ):
                raise ValueError
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        SyntaxError,
        StepFailedError,
    ):
        raise StepFailedError("codex_interface_runtime_required") from None


def physical(ctx: Ctx, policy: ReviewPolicy) -> None:
    """Require the physical target without recapturing a legacy name."""
    terms = policy.experimental_codex
    if not isinstance(terms, ManagedCodexExperiment):
        raise StepFailedError("codex_interface_record_changed")
    try:
        check_host(terms, ctx.data)
    except ValueError as exc:
        raise StepFailedError(str(exc)) from None


def approved(ctx: Ctx, rec: Record, value: InterfaceReceipt) -> None:
    """Recheck exact predecessor artifacts and the approved physical target."""
    runtime(ctx, rec)
    physical(ctx, value.plan.candidate)
    binding, artifacts = legacy_facts(ctx, value.plan.old)
    if (
        binding != value.plan.old_binding
        or artifacts != value.plan.legacy_artifacts_digest
    ):
        raise StepFailedError("codex_interface_legacy_approval_invalid")


def before_start(ctx: Ctx, rec: Record) -> None:
    """Check runtime and physical authority before every managed start."""
    policy = managed_policy(ctx, rec)
    if policy is not None:
        runtime(ctx, rec)
        physical(ctx, policy)
