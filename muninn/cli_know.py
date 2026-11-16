"""Present current knowledge and perform checked explicit local writes."""

from __future__ import annotations

import sqlite3
import time
from argparse import Namespace
from functools import partial
from pathlib import Path

from pydantic import ValidationError

from muninn import knowledge_push, knowledge_read, knowledge_write, query
from muninn.cli_core import (
    Env,
    Out,
    Record,
    Result,
    current_dir,
    read_store,
    write_store,
)
from muninn.cli_decision import decision
from muninn.cli_know_shared import scopes as _scopes
from muninn.cli_learn import installed_policy, propose
from muninn.hook import CODEX_BLOCK_LIMIT
from muninn.hook_frame import BLOCK_LIMIT
from muninn.hook_start import render_page
from muninn.hook_status import scope_label
from muninn.knowledge_models import KnowledgeView
from muninn.knowledge_proof import owner_proofs, proof_view
from muninn.query.answers import event_ref
from muninn.review_models import ProposalInput
from muninn.scope import scope_ids_for_read


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
    ordered = sorted(
        views.values(),
        key=lambda view: (view.created_at, view.id),
        reverse=True,
    )
    out: Out = {
        "knowledge": [view.model_dump(mode="json") for view in ordered]
    }
    # Search is an agent path; only the owner's list shows the full proof.
    if a.know_cmd == "list":
        out["proofs"] = {
            view.id: proof
            for view in ordered
            if (proof := owner_proofs(conn, view))
        }
    return out


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
            "evidence_sources": _evidence_sources(conn, view),
            "proof": owner_proofs(conn, view),
        }
    # Pages follow the session-start block, which has no task scope.
    if a.page is not None and a.task is not None:
        raise ValueError("page_task_unsupported")
    if a.page is None and a.provider is not None:
        raise ValueError("provider_needs_page")
    if a.page is not None:
        return _page(conn, a, env)
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


def _page(conn: sqlite3.Connection, a: Namespace, env: Env) -> Out:
    """Return one page of the session-start memories as plain text."""
    if a.page < 1:
        raise ValueError("page_unavailable")
    cwd = current_dir(env)
    label = scope_label(conn, scope_ids_for_read(conn, cwd), cwd)
    project = next(
        (s for s in _scopes(conn, a, env) if s.project_id is not None), None
    )
    if project is None:
        raise ValueError("scope_unavailable")
    context = knowledge_push.knowledge_context(
        conn, project, None, time.time(), proofs=False
    )
    codex = a.provider == "codex"
    return {
        "banner": render_page(
            context,
            label,
            a.page,
            limit=CODEX_BLOCK_LIMIT if codex else BLOCK_LIMIT,
            flag=" --provider codex" if codex else "",
            show=partial(proof_view, conn),
        )
    }


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
