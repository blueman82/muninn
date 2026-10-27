"""Separate explicit signal-free refusal from legacy account recovery."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from sqlite3 import Connection

from muninn.review_models import ProcessIdentity
from tools.learning_eval.trial_models import AccountCall


def refuse_unclean(
    calls: tuple[AccountCall, ...],
    transaction: Callable[[], AbstractContextManager[Connection]],
    requested: bool,
    *,
    stop: bool = False,
) -> bool:
    """Persist one-way refusal mode and never turn cleanup into resumption."""
    with transaction() as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='subscription_control'"
        ).fetchone()
        if exists is None:
            if not requested:
                return False
            conn.execute(
                "CREATE TABLE subscription_control ("
                "id INTEGER PRIMARY KEY CHECK(id=1), "
                "version INTEGER NOT NULL CHECK(version=1), "
                "stopped INTEGER NOT NULL CHECK(stopped IN (0,1)))"
            )
            conn.execute("INSERT INTO subscription_control VALUES(1,1,0)")
        rows = conn.execute(
            "SELECT id,version,stopped FROM subscription_control"
        ).fetchall()
        if rows not in ([(1, 1, 0)], [(1, 1, 1)]):
            raise ValueError("account_invalid")
        blocked = (
            bool(rows[0][2])
            or stop
            or any(
                not call.cleaned or call.outcome == "account_recovery_required"
                for call in calls
            )
        )
        if blocked:
            conn.execute(
                "UPDATE subscription_control SET stopped=1 WHERE id=1"
            )
    if blocked:
        raise ValueError("account_recovery_required")
    return True


def recover_calls(
    calls: tuple[AccountCall, ...],
    save: Callable[[AccountCall], None],
    recover: Callable[[ProcessIdentity], None],
) -> bool:
    """Retain legacy accounting and verified-identity cleanup."""
    blocked = False
    for call in calls:
        if call.cleaned:
            continue
        updated = call.model_copy(
            update={"state": "unknown", "outcome": "account_recovery_required"}
        )
        save(updated)
        try:
            if call.process_identity is None:
                raise ValueError("process_identity_unverified")
            recover(call.process_identity)
        except (ValueError, OSError):
            blocked = True
        else:
            save(updated.model_copy(update={"cleaned": True}))
    return blocked
