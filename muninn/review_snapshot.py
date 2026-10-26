"""Bind parallel reviews to current knowledge metadata without saving text."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from muninn import store
from muninn.knowledge_models import Scope


def snapshot(conn: sqlite3.Connection, scope: Scope) -> str:
    """Hash same-project revisions, states and unresolved dependencies."""
    knowledge = [
        tuple(row)
        for row in conn.execute(
            "SELECT "
            "k.id,k.current_revision,k.state,k.retracted_at,v.erased_at "
            "FROM knowledge k JOIN knowledge_version v ON v.knowledge_id=k.id "
            "AND v.revision=k.current_revision "
            "WHERE json_extract(v.scope_json,'$.project_id') IS ? "
            "ORDER BY k.id",
            (scope.project_id,),
        )
    ]
    unresolved = [
        tuple(row)
        for row in conn.execute(
            "SELECT u.id,u.state,u.affected_json FROM unresolved u "
            "JOIN review_request r ON r.id=u.request_id "
            "WHERE json_extract(r.scope_json,'$.project_id') IS ? "
            "ORDER BY u.id",
            (scope.project_id,),
        )
    ]
    return hashlib.sha256(
        json.dumps([knowledge, unresolved], sort_keys=True).encode()
    ).hexdigest()


def read(home: Path, scope: Scope) -> str:
    """Close the reader before any prompt-building writer operation."""
    with closing(store.connect_ro(store.db_path(home))) as conn:
        return snapshot(conn, scope)
