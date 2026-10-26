"""Observe stored admission prerequisites without changing authority."""

from __future__ import annotations

import sqlite3

from muninn.review_activation import checked_policy, digest
from muninn.review_models import ReviewAdmission, ReviewPolicy


def policy_status(conn: sqlite3.Connection) -> ReviewAdmission:
    """Read the switch and exact policy integrity in the caller's snapshot.

    Args:
        conn: Existing normalized connection whose transaction is caller-owned.

    Returns:
        Finite control codes without policy contents or process observations.
    """
    row = conn.execute(
        "SELECT admission,policy_json,policy_digest FROM review_control "
        "WHERE singleton=1"
    ).fetchone()
    if row is None:
        return ReviewAdmission(enabled=None, policy_status="control_absent")
    if type(row[0]) is not int or row[0] not in (0, 1):
        return ReviewAdmission(enabled=None, policy_status="control_invalid")
    enabled = bool(row[0])
    if row[1] is None:
        return ReviewAdmission(enabled=enabled, policy_status="policy_absent")
    try:
        policy = checked_policy(ReviewPolicy.model_validate_json(row[1]))
    except ValueError:
        return ReviewAdmission(enabled=enabled, policy_status="policy_invalid")
    status = "valid" if row[2] == digest(policy) else "policy_digest_mismatch"
    return ReviewAdmission(enabled=enabled, policy_status=status)


def admission_check(
    status: ReviewAdmission,
) -> tuple[str, bool | None, str, str]:
    """Report stored prerequisites separately from provider and scan health."""
    if status.enabled is None:
        return "review_admission", None, status.policy_status, "warn"
    valid = status.policy_status == "valid"
    if not status.enabled:
        detail = "closed" if valid else "closed_" + status.policy_status
        return "review_admission", False, detail, "info"
    return (
        "review_admission",
        valid,
        "open" if valid else status.policy_status,
        "info" if valid else "warn",
    )


def configured_policy_check(
    conn: sqlite3.Connection,
) -> tuple[str, bool, str, str]:
    """Preserve the configuration-only check independently of admission."""
    control = conn.execute(
        "SELECT admission,policy_json FROM review_control WHERE singleton=1"
    ).fetchone()
    if control is None:
        return "review_policy", False, "control_absent", "warn"
    configured = control[1] is not None
    valid = configured
    if configured:
        try:
            ReviewPolicy.model_validate_json(control[1])
        except ValueError:
            valid = False
    return (
        "review_policy",
        not control[0] or valid,
        "configured" if valid else "absent_or_invalid",
        "warn",
    )
