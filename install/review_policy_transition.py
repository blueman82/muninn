"""Permit one explicitly reapproved increase to ongoing original-read work."""

from __future__ import annotations

import sqlite3
from contextlib import suppress

from install.context import StepFailedError
from install.review_policy_parse import policy as parse_policy
from muninn.review_json import canonical_json
from muninn.review_models import OngoingBudget, ReviewPolicy
from muninn.review_progress import checked_policy, digest


def compatible(conn: sqlite3.Connection, policy: ReviewPolicy | None) -> None:
    """Check first activation, exact reuse, or the one approved revision."""
    control = conn.execute("SELECT * FROM review_control").fetchone()
    if control is None:
        raise StepFailedError("review_policy_state_invalid")
    if control["policy_json"] is None:
        if (
            control["policy_digest"] is not None
            or control["activated_at"] is not None
        ):
            raise StepFailedError("review_policy_state_invalid")
        if control["admission"] or any(
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("review_source", "review_request")
        ):
            raise StepFailedError("review_policy_state_invalid")
        return
    installed = parse_policy(control["policy_json"])
    if control["policy_digest"] != digest(installed):
        raise StepFailedError("review_policy_state_invalid")
    if policy is not None and digest(policy) != digest(installed):
        check(conn, installed, policy)


def check(
    conn: sqlite3.Connection, installed: ReviewPolicy, selected: ReviewPolicy
) -> None:
    """Require only the recorded v1-to-v2 raw-read allowance change."""
    old = checked_policy(installed)
    new = checked_policy(selected)
    if old.policy_source == new.policy_source == "repository_default":
        if _recovery_refresh(conn, old, new):
            return
        _check_default_refresh(conn, old, new)
        return
    if (
        old.policy_id != "muninn-codex-ongoing-production"
        or new.policy_id != "muninn-codex-ongoing-production-v2"
        or old.max_input_bytes != 65536
        or new.max_input_bytes != 1_048_576
        or not isinstance(old.budget, OngoingBudget)
        or not isinstance(new.budget, OngoingBudget)
        or new.budget.approved_at <= old.budget.approved_at
        or any(
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("review_request", "knowledge")
        )
    ):
        raise StepFailedError("review_policy_transition_required")
    expected = old.model_dump(mode="json")
    expected["policy_id"] = new.policy_id
    expected["max_input_bytes"] = new.max_input_bytes
    expected["budget"]["approved_at"] = new.budget.approved_at
    if expected != new.model_dump(mode="json"):
        raise StepFailedError("review_policy_transition_required")


def _recovery_refresh(
    conn: sqlite3.Connection, old: ReviewPolicy, new: ReviewPolicy
) -> bool:
    """Permit the approved recovery revision and preserve history."""
    if (
        old.policy_id
        not in {
            "muninn-codex-ongoing-production",
            "muninn-codex-ongoing-production-v2",
        }
        or new.policy_id != "muninn-codex-ongoing-production-v3"
    ):
        return False
    if (
        not isinstance(old.budget, OngoingBudget)
        or not isinstance(new.budget, OngoingBudget)
        or new.budget.approved_at <= old.budget.approved_at
    ):
        return False
    if conn.execute(
        "SELECT 1 FROM review_attempt WHERE ended_at IS NULL LIMIT 1"
    ).fetchone():
        return False
    expected = old.model_dump(mode="json")
    expected.update(
        policy_id=new.policy_id,
        max_input_bytes=1_048_576,
        max_attempts=3,
        retry_backoff_seconds=[5.0, 30.0],
        concurrency=4,
    )
    expected["budget"]["approved_at"] = new.budget.approved_at
    return expected == new.model_dump(mode="json")


def _check_default_refresh(
    conn: sqlite3.Connection, old: ReviewPolicy, new: ReviewPolicy
) -> None:
    """Permit refresh before admission has produced learning state."""
    control = conn.execute(
        "SELECT admission FROM review_control WHERE singleton=1"
    ).fetchone()
    if (
        control is None
        or control["admission"] != 1
        or not isinstance(old.budget, OngoingBudget)
        or not isinstance(new.budget, OngoingBudget)
        or new.budget.approved_at <= old.budget.approved_at
        or any(
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("review_request", "knowledge")
        )
        or not _default_bounds_compatible(old, new)
    ):
        raise StepFailedError("review_policy_transition_required")
    expected = old.model_dump(mode="json")
    fresh = new.model_dump(mode="json")
    for key in (
        "policy_id",
        "max_input_bytes",
        "max_rounds",
        "max_output_bytes",
        "context_bytes",
        "deadline_seconds",
    ):
        expected[key] = fresh[key]
    expected["budget"]["approved_at"] = new.budget.approved_at
    if expected != fresh:
        raise StepFailedError("review_policy_transition_required")


def _default_bounds_compatible(old: ReviewPolicy, new: ReviewPolicy) -> bool:
    """Allow the profile revision and reductions in its bounded controls."""
    input_increase = new.max_input_bytes > old.max_input_bytes
    allowed_increase = (
        old.policy_id == "muninn-codex-ongoing-production"
        and new.policy_id == "muninn-codex-ongoing-production-v2"
        and new.max_input_bytes <= 1_048_576
    )
    valid_revision = old.policy_id == new.policy_id or allowed_increase
    return (
        valid_revision
        and (not input_increase or allowed_increase)
        and (
            new.max_rounds <= old.max_rounds
            and new.max_output_bytes <= old.max_output_bytes
            and new.context_bytes <= old.context_bytes
            and new.deadline_seconds <= old.deadline_seconds
        )
    )


def apply(conn: sqlite3.Connection, selected: ReviewPolicy | None) -> None:
    """Replace the selected policy atomically while preserving admission."""
    row = conn.execute(
        "SELECT policy_json,policy_digest FROM review_control"
    ).fetchone()
    if row is None or row["policy_json"] is None or selected is None:
        return
    if row["policy_digest"] == digest(selected):
        return
    if conn.in_transaction:
        raise StepFailedError("review_policy_transition_required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute(
            "SELECT policy_json,policy_digest FROM review_control"
        ).fetchone()
        if current is None or current["policy_json"] is None:
            raise StepFailedError("review_policy_transition_required")
        installed = parse_policy(current["policy_json"])
        if current["policy_digest"] != digest(installed):
            raise StepFailedError("review_policy_state_invalid")
        check(conn, installed, selected)
        if selected.policy_id == "muninn-codex-ongoing-production-v3":
            conn.execute(
                "UPDATE review_request SET "
                "state='cancelled',retry_at=NULL,"
                "cancel_code='policy_replaced' "
                "WHERE state IN ('pending','waiting') AND policy_digest!=?",
                (digest(selected),),
            )
        conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?,"
            "generation=generation+1 WHERE singleton=1",
            (canonical_json(selected), digest(selected)),
        )
        conn.execute("COMMIT")
    except BaseException:
        with suppress(BaseException):
            conn.execute("ROLLBACK")
        raise
