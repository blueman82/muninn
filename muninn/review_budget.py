"""Reserve durable calls against shared account and immutable policy limits."""

from __future__ import annotations

import json
import sqlite3

from muninn.review_charge import Charge, aggregate, limits
from muninn.review_json import canonical_json
from muninn.review_models import (
    CallView,
    ReviewContext,
    ReviewPolicy,
    Usage,
)
from muninn.review_progress import check_window, digest, require_transaction
from muninn.review_read_budget import ReadBudget
from muninn.review_requests import UNKNOWN_CONFIG, request_row, request_view


def call_view(row: sqlite3.Row) -> CallView:
    """Decode a durable call without filling unreported observations."""
    data = dict(row)
    for key in ("requested", "observed", "usage", "process_identity"):
        value = data.pop(f"{key}_json")
        data[key] = json.loads(value) if value is not None else None
    return CallView.model_validate_json(json.dumps(data))


def _context(
    conn: sqlite3.Connection, context: ReviewContext, *, budget: ReadBudget
) -> None:
    """Require the current running attempt and exact admitted policy/window."""
    row = request_row(conn, context.request_id)
    control = conn.execute("SELECT * FROM review_control").fetchone()
    attempt = conn.execute(
        "SELECT * FROM review_attempt WHERE id=?", (context.attempt_id,)
    ).fetchone()
    if (
        row["state"] != "running"
        or row["current_attempt_id"] != context.attempt_id
        or attempt is None
        or attempt["ended_at"] is not None
        or attempt["request_id"] != context.request_id
        or attempt["generation"] != context.generation
        or control["generation"] != context.generation
    ):
        raise ValueError("rejected_fenced")
    policy_digest = digest(context.policy)
    if (
        not control["admission"]
        or control["policy_digest"] != policy_digest
        or row["policy_digest"] != policy_digest
        or canonical_json(context.policy) != row["policy_json"]
        or control["policy_json"] is None
        or canonical_json(
            ReviewPolicy.model_validate_json(control["policy_json"])
        )
        != row["policy_json"]
    ):
        raise ValueError("review_policy_required")
    view = request_view(row)
    if context.scope != view.scope or context.admitted_windows != (
        view.window,
    ):
        raise ValueError("rejected_scope")
    if context.round_index > context.policy.max_rounds:
        raise ValueError("budget_round_limit")
    check_window(conn, view.window, view.scope, context.policy, budget=budget)


def _policies(
    conn: sqlite3.Connection, account: str
) -> tuple[ReviewPolicy, ...]:
    """Reload account snapshots so policy changes cannot reset caps."""
    rows = conn.execute(
        "SELECT DISTINCT r.policy_json FROM review_call c JOIN "
        "review_attempt a "
        "ON a.id=c.attempt_id JOIN review_request r ON r.id=a.request_id "
        "WHERE c.budget_account_id=?",
        (account,),
    )
    return tuple(ReviewPolicy.model_validate_json(row[0]) for row in rows)


def reserve_call(
    conn: sqlite3.Connection,
    context: ReviewContext,
    now: float,
) -> CallView:
    """Persist one idempotent reservation before any wrapper launch."""
    require_transaction(conn, now)
    context = ReviewContext.model_validate_json(canonical_json(context))
    _context(conn, context, budget=ReadBudget(context.policy.max_input_bytes))
    existing = conn.execute(
        "SELECT * FROM review_call WHERE attempt_id=? AND round_index=?",
        (context.attempt_id, context.round_index),
    ).fetchone()
    if existing is not None:
        return call_view(existing)
    policy_digest = digest(context.policy)
    calls = [
        call_view(row)
        for row in conn.execute(
            "SELECT * FROM review_call WHERE budget_account_id=?",
            (context.policy.budget.account_id,),
        )
    ]
    charge = context.policy.budget.conservative_call_cost_decimal
    if any(call.currency != context.policy.budget.currency for call in calls):
        raise ValueError("budget_currency_mismatch")
    for policy in (
        *_policies(conn, context.policy.budget.account_id),
        context.policy,
    ):
        limits([_charge(c) for c in calls], policy.budget, charge, now)
    limits(
        [_charge(c) for c in calls if c.policy_digest == policy_digest],
        context.policy.budget,
        charge,
        now,
    )
    previous = conn.execute(
        "SELECT round_index,state FROM review_call WHERE attempt_id=? "
        "ORDER BY round_index DESC LIMIT 1",
        (context.attempt_id,),
    ).fetchone()
    if previous is not None and previous[1] != "completed":
        raise ValueError("unfinished_round")
    if context.round_index != (previous[0] + 1 if previous else 1):
        raise ValueError("invalid_round_index")
    call = CallView(
        attempt_id=context.attempt_id,
        round_index=context.round_index,
        budget_account_id=context.policy.budget.account_id,
        policy_digest=policy_digest,
        state="reserved",
        reserved_at=now,
        launched_at=None,
        ended_at=None,
        requested=context.policy.runner,
        observed=UNKNOWN_CONFIG,
        usage=None,
        cost_reservation_decimal=charge,
        currency=context.policy.budget.currency,
        process_identity=None,
        outcome=None,
    )
    data = call.model_dump(mode="json")
    for key in ("requested", "observed", "usage", "process_identity"):
        value = data.pop(key)
        data[f"{key}_json"] = json.dumps(value) if value is not None else None
    conn.execute(
        f"INSERT INTO review_call({','.join(data)}) "
        f"VALUES({','.join('?' for _ in data)})",
        tuple(data.values()),
    )
    return call


def aggregate_usage(
    conn: sqlite3.Connection,
    account_id: str,
    policy_digest: str | None = None,
) -> Usage:
    """Sum each measure only when every contributing round reports it."""
    rows = conn.execute(
        "SELECT * FROM review_call WHERE budget_account_id=? "
        "AND (? IS NULL OR policy_digest=?)",
        (account_id, policy_digest, policy_digest),
    ).fetchall()
    calls = [call_view(row) for row in rows]
    return aggregate([_charge(c) for c in calls])


def _charge(call: CallView) -> Charge:
    """Remove admission-only fields before shared conservative arithmetic."""
    return Charge(
        call.state,
        call.reserved_at,
        call.usage,
        call.cost_reservation_decimal,
        call.currency,
    )
