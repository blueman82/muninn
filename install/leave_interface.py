"""Prove that a stopped Codex interface change did not change the rule.

A Codex interface change that stopped at phase ``approved_intent`` did
not change the learning rule, the control row or the published grant
files. The person can leave it, and then make a new preview.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import cast

from install import policy_records
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import store
from muninn.review_json import canonical_json
from muninn.review_progress import digest

PREVIEW = "bin/muninn-install --upgrade --codex-interface preview --check"


def prove(active: Ctx, rec: Record) -> None:
    """Refuse a Codex interface change that can have changed the rule.

    The store is read read-only.

    Args:
        active: The context of the stopped change.
        rec: Its rollback record.

    Raises:
        StepFailedError: ``leave_policy_active`` if the change is past
            its approved intent, or the store does not have the old rule.
    """
    value = policy_records.codex_interface(rec["codex_interface"])
    if (
        value.phase != "approved_intent"
        or value.owner_control is not None
        or value.contained is not None
    ):
        raise StepFailedError("leave_policy_active")
    old, candidate = value.plan.old, value.plan.candidate
    try:
        with closing(store.connect_ro(store.db_path(active.data))) as conn:
            row = conn.execute(
                "SELECT policy_json,policy_digest FROM review_control"
            ).fetchone()
            used = conn.execute(
                "SELECT 1 FROM review_request WHERE policy_digest=? LIMIT 1",
                (digest(candidate),),
            ).fetchone()
    except (store.StoreUnavailableError, sqlite3.Error, OSError):
        raise StepFailedError("leave_policy_active") from None
    if row is None or used is not None:
        raise StepFailedError("leave_policy_active")
    raw, stored = cast(tuple[object, object], tuple(row))
    if raw != canonical_json(old) or stored != digest(old):
        raise StepFailedError("leave_policy_active")


def plan(ctx: Ctx, rec: Record) -> None:
    """Say what Muninn found and what the leave does, with no path.

    Args:
        ctx: The context of this run.
        rec: The record of the stopped change.
    """
    ctx.say(
        "Muninn found an unfinished Codex interface change from"
        f" {rec['ts']}, for commit {rec['sha'][:7]}."
    )
    ctx.say("The change stopped before it changed the learning rule.")
    ctx.say(
        "Muninn will mark it as left. It keeps the install record, and it"
        " removes no file."
    )
    ctx.say("It changes no link, no settings file, no service and no store.")
    ctx.say("The Muninn service stays stopped until the next install.")


def done(ctx: Ctx) -> None:
    """Tell the person the next command after the leave.

    Args:
        ctx: The context of this run.
    """
    ctx.say(
        "Muninn left the unfinished Codex interface change. To make a new"
        f" plan, run {PREVIEW}"
    )
