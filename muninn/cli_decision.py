"""Connect explicit conversational commands to checked decision APIs."""

from __future__ import annotations

import sqlite3
import time
from argparse import Namespace
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from muninn import knowledge_read, query
from muninn.cli_core import Env, Out, Record, Result, read_store, write_store
from muninn.cli_know_shared import saved_policy, scopes
from muninn.cli_output import redacted
from muninn.decision_candidates import show_candidate, stage_candidate
from muninn.decision_models import CandidateInput, DecisionCode, DecisionIntent
from muninn.decision_proof import verify_decision
from muninn.decision_read import list_unresolved, show_unresolved
from muninn.decision_requests import request_application
from muninn.decision_write import accept_decision, decision_view, submit_intent
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_read_budget import EvidenceLimitError, ReadBudget


def _input[T: CandidateInput | DecisionIntent](path: str, model: type[T]) -> T:
    """Validate a strict file without logging its contents or path."""
    try:
        return model.model_validate_json(Path(path).read_bytes())
    except ValidationError:
        raise ValueError("rejected_validation") from None


def _code(exc: ValueError) -> str:
    """Expose only finite program-owned refusal codes."""
    value = exc.code if isinstance(exc, EvidenceLimitError) else str(exc)
    if value in {"review_policy_required", "unresolved_limit"}:
        return value
    try:
        return TypeAdapter(DecisionCode).validate_python(value)
    except ValueError:
        return "decision_invalid"


def _unchanged(out: Out) -> Out:
    """Refuse a display contract that current output redaction would alter."""
    if redacted(out) != out:
        raise ValueError("candidate_changed")
    return out


def _submit(
    conn: sqlite3.Connection,
    home: Path,
    intent: DecisionIntent,
    now: float,
    policy: ReviewPolicy,
) -> Out:
    """Retain intent and roll back proof work before finite pending status."""
    view = submit_intent(conn, intent, now)
    if view.state in {"saved", "erased", "rejected", "superseded"}:
        return view.model_dump(mode="json")
    conn.execute("SAVEPOINT cli_memory_proof")
    try:
        proof = verify_decision(
            conn, home, intent, policy, ReadBudget(policy.max_input_bytes)
        )
        accept_decision(conn, proof, now, policy=policy)
    except ValueError as exc:
        conn.execute("ROLLBACK TO cli_memory_proof")
        code = _code(exc)
        conn.execute(
            "UPDATE memory_decision SET state=?,code=? "
            "WHERE candidate_id=? AND locator_json=? "
            "AND accepted_revision IS NULL AND state!='erased'",
            (
                (
                    "waiting_verification"
                    if code == "waiting_index"
                    else "blocked"
                ),
                code,
                intent.candidate_id,
                canonical_json(intent),
            ),
        )
    finally:
        conn.execute("RELEASE cli_memory_proof")
    return decision_view(conn, intent.candidate_id).model_dump(mode="json")


def _write(
    conn: sqlite3.Connection,
    home: Path,
    value: CandidateInput | DecisionIntent,
) -> Out:
    """Own the SQL transaction inside the one CLI writer root."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        policy = saved_policy(conn)
        now = time.time()
        if isinstance(value, CandidateInput):
            out = stage_candidate(conn, home, value, now).model_dump(
                mode="json"
            )
        else:
            out = _submit(conn, home, value, now, policy)
        _unchanged(out)
        conn.commit()
        return out
    except BaseException:
        conn.rollback()
        raise


@query.guarded
def _read(conn: sqlite3.Connection, a: Namespace, env: Env) -> Out:
    """Return explicit views without admitting evidence or changing rows."""
    with knowledge_read.read_snapshot(conn):
        policy = saved_policy(conn)
        if a.know_cmd == "unresolved":
            if a.unresolved_id is not None:
                return show_unresolved(conn, a.unresolved_id).model_dump(
                    mode="json"
                )
            return {
                "unresolved": [
                    view.model_dump(mode="json")
                    for view in list_unresolved(
                        conn,
                        tuple(scopes(conn, a, env)),
                        limit=a.limit,
                        after=a.after,
                    )
                ]
            }
        if a.decision_cmd == "show":
            return _unchanged(
                show_candidate(
                    conn, a.decision_id, policy.context_bytes
                ).model_dump(mode="json")
            )
        return _unchanged(
            decision_view(conn, a.decision_id).model_dump(mode="json")
        )


def _human(a: Namespace, out: Out) -> Out:
    """Explain actual state while preserving default typed JSON fields."""
    if not getattr(a, "human", False):
        return out
    if a.know_cmd == "unresolved":
        text = "These are recorded questions. Opening them gives no approval."
    elif a.decision_cmd == "show":
        text = (
            "Show the owner the exact complete text field as the final "
            "message. "
        )
        text += (
            "Add no prose, fence, or newline. The agent handles all checks."
        )
    else:
        meanings = {
            "waiting_verification": (
                "Muninn has not verified the original reply. The decision "
                "remains pending."
            ),
            "approved_pending": (
                "Muninn verified the decision. The memory has not yet been "
                "saved."
            ),
            "saved": (
                "This memory was saved. Its existing receipt remains valid."
                " A later change needs separate checked retraction or "
                "replacement."
            ),
            "rejected": (
                "Muninn verified rejection. This decision saved no "
                "knowledge."
            ),
            "blocked": (
                "A check blocked this decision. The memory has not been "
                "saved by this decision."
            ),
            "superseded": (
                "A new draft replaced this draft. The new draft needs its "
                "own approval."
            ),
            "erased": "This proposal was erased. Its content is unavailable.",
        }
        text = meanings[out["state"]]
    return out | {"explanation": text}


@query.guarded
def decision(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Dispatch checked operations with apply outside other writer roots."""
    record["cmd"] = f"know {a.know_cmd}"
    try:
        command = getattr(a, "decision_cmd", None)
        if command in {"stage", "submit"}:
            model = CandidateInput if command == "stage" else DecisionIntent
            value = _input(a.input, model)
            out = write_store(home, lambda conn: _write(conn, home, value))
        elif command == "apply":
            out = request_application(
                home, a.decision_id, time.time()
            ).model_dump(mode="json")
        else:
            out = read_store(home, lambda conn: _read(conn, a, env))
        return 0, _human(a, out)
    except ValueError as exc:
        return 2, {"error": _code(exc)}
