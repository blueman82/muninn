"""Present current knowledge and perform checked explicit local writes."""

from __future__ import annotations

import sqlite3
import time
from argparse import Namespace
from pathlib import Path

from pydantic import ValidationError

from muninn import knowledge_push, knowledge_read, knowledge_write, query
from muninn.cli_core import (
    Env,
    Out,
    Record,
    Result,
    read_store,
    write_store,
)
from muninn.cli_decision import decision
from muninn.cli_know_shared import scopes as _scopes
from muninn.cli_learn import installed_policy, propose
from muninn.knowledge_models import KnowledgeView
from muninn.query.answers import event_ref
from muninn.review_models import ProposalInput


def _views(conn: sqlite3.Connection, a: Namespace, env: Env) -> Out:
    """List or search only current applicable supported guidance."""
    views: dict[str, KnowledgeView] = {}
    with knowledge_read.read_snapshot(conn):
        for scope in _scopes(conn, a, env):
            found = (
                knowledge_read.search_knowledge(conn, a.query, scope)
                if a.know_cmd == "search"
                else knowledge_read.current_knowledge(conn, scope, time.time())
            )
            views.update((view.id, view) for view in found)
    return {
        "knowledge": [
            view.model_dump(mode="json")
            for view in sorted(
                views.values(),
                key=lambda view: (view.created_at, view.id),
                reverse=True,
            )
        ]
    }


def _evidence_sources(
    conn: sqlite3.Connection, view: KnowledgeView
) -> list[dict[str, str | None]]:
    """Resolve retained support to exact original refs for this revision."""
    if view.body is None:
        return []
    rows = conn.execute(
        "SELECT DISTINCT s.evidence_id,e.provider,e.thread_id,e.line,"
        "e.part,e.line_sha256 FROM knowledge_support s "
        "LEFT JOIN review_evidence e ON e.id=s.evidence_id "
        "AND e.state='live' WHERE s.knowledge_id=? AND s.revision=? "
        "ORDER BY s.evidence_id",
        (view.id, view.revision),
    )
    return [
        {
            "evidence_id": row["evidence_id"],
            "ref": event_ref(row) if row["provider"] is not None else None,
            "line_sha256": row["line_sha256"],
        }
        for row in rows
    ]


# Translate within read_store so its one-time hot-journal healing can run.
@query.guarded
def _read(conn: sqlite3.Connection, a: Namespace, env: Env) -> Out:
    """Return typed views or context bounded by the installed policy."""
    if a.know_cmd in {"list", "search"}:
        return _views(conn, a, env)
    if a.know_cmd == "show":
        view = knowledge_read.show_knowledge(conn, a.kid, a.revision)
        return view.model_dump(mode="json") | {
            "evidence_sources": _evidence_sources(conn, view)
        }
    policy = installed_policy(conn)
    scopes = _scopes(conn, a, env)
    project = next(
        (scope for scope in scopes if scope.project_id is not None), None
    )
    if project is None:
        raise ValueError("scope_unavailable")
    return knowledge_push.knowledge_context(
        conn, project, policy.context_bytes, time.time()
    ).model_dump(mode="json")


def _proposal(a: Namespace) -> ProposalInput:
    """Validate the explicit content contract without allocating authority."""
    try:
        return ProposalInput.model_validate_json(Path(a.input).read_bytes())
    except ValidationError:
        raise ValueError("rejected_validation") from None


@query.guarded
def know(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Dispatch current knowledge controls and emit safe refusal codes."""
    if a.know_cmd in {"decision", "unresolved"}:
        return decision(a, env, home, record)
    record["cmd"] = f"know {a.know_cmd}"
    try:
        if a.know_cmd == "propose":
            proposal = _proposal(a)
            out = propose(home, proposal).model_dump(mode="json")
            record["knowledge_ids"] = [
                item["knowledge_id"] for item in out["applied"]
            ]
        elif a.know_cmd == "retract":
            out = write_store(
                home,
                lambda conn: knowledge_write.retract_knowledge(
                    conn, a.kid, a.expected_revision, a.reason, time.time()
                ).model_dump(mode="json"),
            )
            record["knowledge_ids"] = [out["id"]]
        else:
            out = read_store(home, lambda conn: _read(conn, a, env))
            record["knowledge_ids"] = [
                view["id"]
                for view in out.get("knowledge", out.get("notes", []))
            ]
            if "id" in out:
                record["knowledge_ids"] = [out["id"]]
    except ValidationError:
        return 2, {"error": "rejected_validation"}
    except ValueError as exc:
        code = (
            "rejected_evidence"
            if str(exc)
            in {
                "original_unavailable",
                "evidence_changed",
                "evidence_unavailable",
                "evidence_limit",
            }
            else str(exc)
        )
        return 2, {"error": code}
    return 0, out
