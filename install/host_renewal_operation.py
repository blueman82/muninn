"""Bind one actual installer acquisition to its approved lock observation."""

from __future__ import annotations

import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import TypeAdapter

from install import legacy_baseline as baseline
from install import legacy_state
from install.context import Ctx, StepFailedError
from install.host_renewal_binding_models import HeldOperation as HeldOperation
from install.legacy_state import OperationCheck
from install.policy_record_models import FileState

_ACTIVE: ContextVar[OperationCheck | None] = ContextVar(
    "host_renewal_operation", default=None
)


_BOUND: ContextVar[HeldOperation | None] = ContextVar(
    "host_renewal_held", default=None
)


def observe_operation(ctx: Ctx) -> FileState | None:
    """Observe the exact private lock without creating or acquiring it."""
    path = ctx.lib / ".install-record.lock"
    return (
        TypeAdapter(FileState).validate_python(
            baseline.file_state(path, private=True)
        )
        if os.path.lexists(path)
        else None
    )


def validate_operation(ctx: Ctx, operation: HeldOperation) -> None:
    """Reject fabricated, expired or changed held evidence."""
    if _BOUND.get() is not operation or _ACTIVE.get() is not operation.check:
        raise StepFailedError("host_renewal_approval_required")
    if operation.check() != operation.identity:
        raise StepFailedError("host_renewal_state_changed")
    if observe_operation(ctx) != operation.held:
        raise StepFailedError("host_renewal_state_changed")
    if operation.check() != operation.identity:
        raise StepFailedError("host_renewal_state_changed")


def bind_operation(
    ctx: Ctx,
    *,
    approved: FileState | None,
    expected_before: FileState | None,
    before: FileState | None,
    check: OperationCheck,
) -> HeldOperation:
    """Permit only the immediate witnessed regular-file mode transition."""
    if _ACTIVE.get() is not check:
        raise StepFailedError("host_renewal_approval_required")
    if before != expected_before or before is None:
        raise StepFailedError("host_renewal_state_changed")
    identity = check()
    held = observe_operation(ctx)
    if held is None:
        raise StepFailedError("host_renewal_state_changed")
    old, new = before["stamp"], held["stamp"]
    if (
        old[:2] != list(identity)
        or new[:2] != list(identity)
        or old[3:5] != new[3:5]
        or before["sha256"] != held["sha256"]
        or old[2] not in {stat.S_IFREG | 0o600, stat.S_IFREG | 0o700}
        or (old[2] == stat.S_IFREG | 0o600 and old != new)
        or new[2] != stat.S_IFREG | 0o600
    ):
        raise StepFailedError("host_renewal_state_changed")
    result = HeldOperation(approved, before, held, identity, check)
    if observe_operation(ctx) != held or check() != identity:
        raise StepFailedError("host_renewal_state_changed")
    return result


@contextmanager
def held_operation(
    ctx: Ctx, *, approved: FileState | None, expected_before: FileState | None
) -> Generator[HeldOperation]:
    """Retain the existing operation once and bind its actual check closure.

    Args:
        ctx: The mutating installer context.
        approved: The initial owner-approved pre-acquisition observation.
        expected_before: The initial or last durable held observation.

    Yields:
        Transient proof valid only within this operation.

    Raises:
        StepFailedError: If prior absence or drift cannot be proved safe.
    """
    before = observe_operation(ctx)
    if before != expected_before:
        raise StepFailedError(
            "host_renewal_state_changed: installer lock evidence differs "
            "from the last approved or durable observation. No automatic "
            "continuation is supported. Preserve the receipt and state; "
            "an operator or maintainer must review this metadata gap."
        )
    if (
        _ACTIVE.get() is not None
        or ctx.dry_run
        or before is None
        or before != expected_before
    ):
        raise StepFailedError("host_renewal_state_changed")
    with legacy_state.operation(ctx) as check:
        token = _ACTIVE.set(check)
        try:
            operation = bind_operation(
                ctx,
                approved=approved,
                expected_before=expected_before,
                before=before,
                check=check,
            )
            bound = _BOUND.set(operation)
            try:
                validate_operation(ctx, operation)
                yield operation
                validate_operation(ctx, operation)
            finally:
                _BOUND.reset(bound)
        finally:
            _ACTIVE.reset(token)
