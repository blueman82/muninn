"""Expose explicit bounded requests and checked local proposal attempts."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from argparse import Namespace
from pathlib import Path

from pydantic import ValidationError

from muninn import query, review_recovery, review_requests
from muninn.cli_core import Env, Out, Record, Result, read_store, write_store
from muninn.cli_learn_explain import explain
from muninn.cli_learning_setup import failure as setup_failure
from muninn.cli_learning_setup import setup
from muninn.knowledge_models import RequestId, Scope
from muninn.query.answers import BadArgumentError
from muninn.query.filters import resolve_session
from muninn.review_budget import call_view
from muninn.review_commit import CommitContext, commit_review
from muninn.review_evidence_models import (
    EvidenceRecord,
    EvidenceWindow,
    SourceRef,
)
from muninn.review_json import (
    canonical_json,
    finite_float,
    reject_constant,
    unique_object,
)
from muninn.review_models import (
    ProposalInput,
    Receipt,
    ReviewDetail,
    ReviewPolicy,
)
from muninn.review_progress import (
    boundary,
    checked_policy,
    digest,
    source_progress,
)
from muninn.review_request_views import attempt_view, request_row, request_view


def installed_policy(conn: sqlite3.Connection) -> ReviewPolicy:
    """Require the active frozen policy snapshot and its exact digest."""
    row = conn.execute(
        "SELECT * FROM review_control WHERE singleton=1"
    ).fetchone()
    if row is None or not row["admission"] or row["policy_json"] is None:
        raise ValueError("review_policy_required")
    try:
        policy = checked_policy(
            ReviewPolicy.model_validate_json(row["policy_json"])
        )
    except ValueError:
        raise ValueError("review_policy_required") from None
    if row["policy_digest"] != digest(policy):
        raise ValueError("review_policy_required")
    return policy


def _policy(conn: sqlite3.Connection, path: str | None) -> ReviewPolicy:
    """Read an explicit strict policy or require the installed snapshot."""
    installed = installed_policy(conn)
    if path is None:
        return installed
    try:
        policy = checked_policy(
            ReviewPolicy.model_validate_json(Path(path).read_bytes())
        )
    except ValueError:
        raise ValueError("review_policy_required") from None
    if canonical_json(policy) != canonical_json(installed):
        raise ValueError("review_policy_required")
    return policy


def _source(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """Resolve one indexed primary thread, rejecting ambiguous sessions."""
    provider, separator, thread = ref.partition(":")
    if separator:
        rows = conn.execute(
            "SELECT * FROM source WHERE provider=? AND thread_id=?",
            (provider, thread),
        ).fetchall()
    else:
        root = resolve_session(conn, ref)
        rows = conn.execute(
            "SELECT * FROM source WHERE session_root=? AND "
            "thread_class='primary'",
            (root,),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("source_unavailable")
    return rows[0]


def _window(
    conn: sqlite3.Connection, a: Namespace, now: float
) -> tuple[EvidenceWindow, Scope]:
    """Choose explicit paired bounds or the uncompleted activation interval."""
    if (a.from_line is None) != (a.through_line is None):
        raise ValueError("paired_bounds_required")
    source = _source(conn, a.session)
    last, _ = boundary(conn, source)
    first, through = a.from_line, a.through_line
    if first is None:
        progress = source_progress(conn, source, now)
        first = (
            max(progress["activation_line"], progress["completed_line"]) + 1
        )
        through = last
    end = conn.execute(
        "SELECT line_sha256 FROM event WHERE source_id=? AND line=? LIMIT 1",
        (source["id"], through),
    ).fetchone()
    if end is None or first > through:
        raise ValueError("incomplete_window")
    scopes = conn.execute(
        "SELECT DISTINCT sc.key,sc.kind FROM event e JOIN scope sc ON "
        "sc.id=e.scope_id WHERE e.source_id=? AND e.line BETWEEN ? AND ?",
        (source["id"], first, through),
    ).fetchall()
    if len(scopes) != 1:
        raise ValueError("rejected_scope")
    return EvidenceWindow(
        source=SourceRef(
            provider=source["provider"], thread_id=source["thread_id"]
        ),
        from_line=first,
        through_line=through,
        boundary_hash=end[0],
    ), Scope(
        project_id=None if scopes[0]["kind"] == "global" else scopes[0]["key"],
        task=None,
    )


def _request(conn: sqlite3.Connection, a: Namespace, now: float) -> Out:
    """Admit replay and ordinary windows through the same explicit queue."""
    policy = _policy(conn, a.policy)
    window, scope = _window(conn, a, now)
    task = getattr(a, "task", None)
    if task is not None:
        if (
            scope.project_id is None
            or len(task) > 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", task) is None
        ):
            raise ValueError("rejected_scope")
        scope = Scope(project_id=scope.project_id, task=task)
    return review_requests.enqueue_review(
        conn, window, scope, policy, "explicit", now
    ).model_dump(mode="json")


def _resume(conn: sqlite3.Connection, a: Namespace, now: float) -> Out:
    """Reopen only the inspected contained refusal under exact local terms."""
    try:
        raw = Path(a.policy).read_bytes()
        json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
        policy = checked_policy(ReviewPolicy.model_validate_json(raw))
    except (OSError, UnicodeError, ValueError):
        raise ValueError("recovery_refused") from None
    review_requests.resume_admission(conn, policy, a.failed_request, now)
    return {"admission": True, "policy_id": policy.policy_id}


def _receipt(conn: sqlite3.Connection, ident: RequestId) -> Receipt | None:
    """Render the unique durable receipt without altering request state."""
    row = conn.execute(
        "SELECT * FROM review_receipt WHERE request_id=?", (ident,)
    ).fetchone()
    if row is None:
        return None
    return Receipt.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": row["request_id"],
                "attempt_id": row["attempt_id"],
                "applied": json.loads(row["applied_json"]),
                "unresolved_ids": json.loads(row["unresolved_json"]),
                "committed_at": row["committed_at"],
                "outcome": row["outcome"],
                "no_change_reason": row["no_change_reason"],
            }
        )
    )


def _evidence(row: sqlite3.Row) -> EvidenceRecord:
    """Render governed evidence only for explicitly requested detail."""
    data = dict(row)
    for key in ("scope", "selector", "original_locator"):
        data[key] = json.loads(data.pop(f"{key}_json"))
    return EvidenceRecord.model_validate_json(json.dumps(data))


def _detail(conn: sqlite3.Connection, ident: RequestId) -> Out:
    """Project durable request, attempt, call, evidence and receipt history."""
    return ReviewDetail(
        request=request_view(request_row(conn, ident)),
        attempts=[
            attempt_view(conn, row[0])
            for row in conn.execute(
                "SELECT id FROM review_attempt WHERE request_id=? ORDER BY "
                "started_at,id",
                (ident,),
            )
        ],
        calls=[
            call_view(row)
            for row in conn.execute(
                "SELECT c.* FROM review_call c JOIN review_attempt a ON "
                "a.id=c.attempt_id WHERE a.request_id=? ORDER BY "
                "a.started_at,c.round_index",
                (ident,),
            )
        ],
        evidence=[
            _evidence(row)
            for row in conn.execute(
                "SELECT e.* FROM review_evidence e JOIN review_input i ON "
                "i.evidence_id=e.id WHERE i.request_id=? ORDER BY "
                "e.line,e.part,e.id",
                (ident,),
            )
        ],
        receipt=_receipt(conn, ident),
        diagnostics=review_recovery.details(conn, ident),
    ).model_dump(mode="json")


# Translate within read_store so its one-time hot-journal healing can run.
@query.guarded
def _read(conn: sqlite3.Connection, a: Namespace) -> Out:
    """Return the requested durable detail or optionally filtered list."""
    if a.learn_cmd == "show":
        return _detail(conn, a.request_id)
    if a.learn_cmd == "explain":
        return explain(conn, a.request_id)
    rows = conn.execute(
        "SELECT * FROM review_request WHERE (? IS NULL OR state=?) ORDER BY "
        "created_at,id",
        (a.state, a.state),
    )
    return {
        "requests": [request_view(row).model_dump(mode="json") for row in rows]
    }


def _write(conn: sqlite3.Connection, a: Namespace) -> Out:
    """Own one queue transaction without model or process execution."""
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if a.learn_cmd == "setup":
            out = setup(conn, a, installed_policy(conn), now)
        elif a.learn_cmd in {"request", "replay"}:
            out = _request(conn, a, now)
        elif a.learn_cmd == "resume":
            out = _resume(conn, a, now)
        elif a.learn_cmd == "dismiss":
            request_row(conn, a.request_id)
            out = {
                "request_id": a.request_id,
                "dismissed": review_recovery.clear(conn, a.request_id),
            }
        else:
            operation = (
                review_requests.cancel_request
                if a.learn_cmd == "cancel"
                else review_requests.retry_request
            )
            out = operation(conn, a.request_id, now).model_dump(mode="json")
        conn.commit()
        return out
    except BaseException:
        conn.rollback()
        raise


def _claim_proposal(
    conn: sqlite3.Connection, home: Path, proposal: ProposalInput
) -> CommitContext:
    """Claim the current local attempt with complete durable input IDs."""
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        generation = conn.execute(
            "SELECT generation FROM review_control"
        ).fetchone()[0]
        attempt = review_requests.claim_proposal(
            conn, proposal.request_id, generation, now
        )
        evidence = tuple(
            row[0]
            for row in conn.execute(
                "SELECT evidence_id FROM review_input WHERE request_id=? "
                "ORDER BY evidence_id",
                (proposal.request_id,),
            )
        )
        context = CommitContext(
            home=home,
            request_id=proposal.request_id,
            attempt_id=attempt.id,
            generation=attempt.generation,
            evidence_ids=evidence,
            now=now,
        )
        conn.commit()
        return context
    except BaseException:
        conn.rollback()
        raise


@query.guarded
def propose(home: Path, proposal: ProposalInput) -> Receipt:
    """Claim and checked-commit under separate, nonnested writer roots."""
    context = write_store(
        home, lambda conn: _claim_proposal(conn, home, proposal)
    )
    return write_store(
        home,
        lambda conn: commit_review(
            conn,
            context.model_copy(update={"now": time.time()}),
            proposal.result,
        ),
    )


@query.guarded
def learn(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Dispatch explicit learning controls with content-free refusal codes."""
    record["cmd"] = f"learn {a.learn_cmd}"
    try:
        if a.learn_cmd == "setup" and a.mode == "all" and a.confirm is None:
            out = read_store(
                home,
                lambda conn: setup(
                    conn, a, installed_policy(conn), time.time()
                ),
            )
        elif a.learn_cmd in {"list", "show", "explain"}:
            out = read_store(home, lambda conn: _read(conn, a))
        else:
            out = write_store(home, lambda conn: _write(conn, a))
    except ValidationError:
        return 2, {"error": "rejected_validation"}
    except (ValueError, BadArgumentError) as exc:
        if a.learn_cmd == "setup":
            return 2, setup_failure(str(exc))
        return 2, {"error": str(exc)}
    return 0, out
