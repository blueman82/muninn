"""Read checked knowledge scope and saved policy without new authority."""

from __future__ import annotations

import sqlite3
from argparse import Namespace

from muninn.cli_core import Env, current_dir
from muninn.knowledge_models import Scope
from muninn.query.answers import knowledge_scopes
from muninn.review_models import ReviewPolicy
from muninn.review_progress import checked_policy, digest
from muninn.scope import scope_ids_for_read


def scopes(conn: sqlite3.Connection, a: Namespace, env: Env) -> list[Scope]:
    """Resolve permitted mapped project/task scopes without writing paths."""
    ids = scope_ids_for_read(conn, current_dir(env))
    return knowledge_scopes(
        conn, ids, getattr(a, "all_projects", False), getattr(a, "task", None)
    )


def saved_policy(conn: sqlite3.Connection) -> ReviewPolicy:
    """Require the actual strict saved policy and digest even while paused."""
    row = conn.execute(
        "SELECT * FROM review_control WHERE singleton=1"
    ).fetchone()
    if row is None or row["policy_json"] is None:
        raise ValueError("review_policy_required")
    try:
        policy = checked_policy(
            ReviewPolicy.model_validate_json(row["policy_json"])
        )
        if row["policy_digest"] != digest(policy):
            raise ValueError("review_policy_required")
    except ValueError:
        raise ValueError("review_policy_required") from None
    return policy
