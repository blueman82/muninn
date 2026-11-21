"""Read checked knowledge scope without new authority."""

from __future__ import annotations

import sqlite3
from argparse import Namespace

from muninn.cli_core import Env, current_dir
from muninn.knowledge_models import Scope
from muninn.query.answers import knowledge_scopes
from muninn.scope import scope_ids_for_read


def scopes(conn: sqlite3.Connection, a: Namespace, env: Env) -> list[Scope]:
    """Resolve permitted mapped project/task scopes without writing paths."""
    ids = scope_ids_for_read(conn, current_dir(env))
    return knowledge_scopes(
        conn, ids, getattr(a, "all_projects", False), getattr(a, "task", None)
    )
