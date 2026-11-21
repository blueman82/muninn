"""Stand in for the constant review policy with a fixture policy."""

from __future__ import annotations

import contextlib
import unittest
from importlib import import_module
from unittest import mock

from muninn import review_settings
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest

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
