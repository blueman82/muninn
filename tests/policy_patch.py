"""Stand in for the constant review policy with a fixture policy."""

from __future__ import annotations

import contextlib
import sqlite3
import unittest
from importlib import import_module
from unittest import mock

from muninn import review_settings
from muninn.review_claims import UNKNOWN_CONFIG
from muninn.review_json import canonical_json
from muninn.review_models import CallView, ReviewContext, ReviewPolicy
from muninn.review_progress import digest
from muninn.review_request_views import call_view

_CONSUMERS = (
    "cli_decision",
    "cli_know",
    "cli_learn",
    "decision_candidates",
    "decision_requests",
    "hook_learn",
    "knowledge_read",
    "review_claims",
    "review_commit",
    "review_learner",
    "review_requests",
)


def patched_policy(policy: ReviewPolicy) -> contextlib.ExitStack:
    """Make every consumer read this policy until the stack closes.

    Args:
        policy: The fixture policy to use in place of the constant.

    Returns:
        The open stack; closing it restores the constant.
    """
    stack = contextlib.ExitStack()
    for name in _CONSUMERS:
        module = import_module(f"muninn.{name}")
        stack.enter_context(mock.patch.object(module, "POLICY", policy))
    return stack


def use_policy(case: unittest.TestCase, policy: ReviewPolicy) -> None:
    """Use this policy for the whole test and turn learning on.

    Args:
        case: The running test; the patches end with it.
        policy: The fixture policy to use in place of the constant.
    """
    case.enterContext(patched_policy(policy))
    # The status readers still look at the stored control row.
    conn = getattr(case, "conn", None)
    if conn is not None:
        conn.execute(
            "UPDATE review_control SET admission=1,policy_json=?,"
            "policy_digest=?",
            (canonical_json(policy), digest(policy)),
        )
    home = getattr(case, "home", None)
    if home is not None:
        review_settings.save(home, review_settings.Settings(enabled=True))


def reserve_call(
    conn: sqlite3.Connection, context: ReviewContext, now: float
) -> CallView:
    """Insert the reserved call row a learning round would record.

    Args:
        conn: Connection in the caller's transaction.
        context: Admitted round to record.
        now: Reservation time in UTC seconds.

    Returns:
        The stored call.
    """
    conn.execute(
        "INSERT OR IGNORE INTO review_call(attempt_id,round_index,"
        "budget_account_id,"
        "policy_digest,state,reserved_at,requested_json,observed_json) "
        "VALUES(?,?,'test',?,'reserved',?,'{}',?)",
        (
            context.attempt_id,
            context.round_index,
            digest(context.policy),
            now,
            canonical_json(UNKNOWN_CONFIG),
        ),
    )
    return call_view(
        conn.execute(
            "SELECT * FROM review_call WHERE attempt_id=? AND round_index=?",
            (context.attempt_id, context.round_index),
        ).fetchone()
    )
