"""Admit distinct memory applications and continue checked locator intents."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from muninn.decision_models import (
    DecisionCode,
    DecisionId,
    DecisionIntent,
    DecisionView,
    VerifiedDecision,
)
from muninn.decision_proof import verify_decision
from muninn.decision_write import (
    accept_decision,
    decision_view,
    submit_intent,
)
from muninn.knowledge_write import target
from muninn.review_commit import EVIDENCE_ERRORS
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import RequestView, ReviewPolicy
from muninn.review_progress import (
    check_window,
    checked_policy,
    digest,
    require_transaction,
)
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from muninn.review_request_views import request_row, request_view
from muninn.review_supervisor_store import database


def _application_key(ident: DecisionId, frozen: str, revision: int) -> str:
    """Hash only the frozen candidate and accepted revision in a new domain."""
    payload = canonical_json_value(
        {
            "candidate_id": ident,
            "candidate_digest": frozen,
            "decision_revision": revision,
        }
    ).encode()
    result = hashlib.sha256()
    for part in (b"muninn-memory-application-v1", payload):
        result.update(len(part).to_bytes(8, "big"))
        result.update(part)
    return result.hexdigest()


def _policy(conn: sqlite3.Connection) -> ReviewPolicy:
    """Load the current presentation and physical limits even while paused."""
    row = conn.execute("SELECT * FROM review_control").fetchone()
    if row is None or row["policy_json"] is None:
        raise ValueError("rejected_validation")
    policy = checked_policy(
        ReviewPolicy.model_validate_json(row["policy_json"])
    )
    if digest(policy) != row["policy_digest"]:
        raise ValueError("rejected_validation")
    return policy


def _approval(
    conn: sqlite3.Connection, ident: DecisionId, revision: int
) -> sqlite3.Row:
    """Resolve authority through the active accepted history pointer."""
    view = decision_view(conn, ident)
    row = conn.execute(
        "SELECT d.*,c.application_request_id FROM memory_candidate c "
        "JOIN memory_decision d "
        "ON d.id=c.active_decision_id AND d.candidate_id=c.id "
        "AND d.candidate_digest=c.digest AND d.accepted_revision=c.revision "
        "WHERE c.id=? AND c.state='approved_pending' AND c.revision=? "
        "AND d.state='verified' AND d.action='approve' "
        "AND d.accepted_revision>0 AND d.owner_evidence_id IS NOT NULL "
        "AND d.display_evidence_id IS NOT NULL",
        (ident, revision),
    ).fetchone()
    if type(revision) is not int or row is None or view.candidate is None:
        raise ValueError("rejected_fenced")
    return row


def _insert_application(
    conn: sqlite3.Connection,
    view: DecisionView,
    proof: VerifiedDecision,
    policy: ReviewPolicy,
    source: sqlite3.Row,
    now: float,
) -> RequestView:
    """Retain the owner window and inputs without ordinary coverage."""
    candidate = view.candidate
    if candidate is None:
        raise ValueError("decision_erased")
    request_id = f"R_{uuid4().hex}"
    window = proof.owner_window
    conn.execute(
        "INSERT INTO review_request(id,duplicate_key,provider,thread_id,"
        "session_root,from_line,through_line,boundary_hash,scope_json,"
        "policy_json,policy_digest,trigger,origin,state,created_at,"
        "candidate_id,decision_revision) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,'explicit','proposal','pending',?,?,?)",
        (
            request_id,
            _application_key(view.id, view.digest, view.revision),
            window.source.provider,
            window.source.thread_id,
            source["session_root"],
            window.from_line,
            window.through_line,
            window.boundary_hash,
            canonical_json(candidate.scope),
            canonical_json(policy),
            digest(policy),
            now,
            view.id,
            view.revision,
        ),
    )
    ids = (
        *candidate.original_evidence_ids,
        proof.owner_evidence_id,
        proof.display_evidence_id,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO review_input VALUES(?,?)",
        [(request_id, evidence) for evidence in ids],
    )
    conn.execute(
        "UPDATE memory_candidate SET application_request_id=?,code=NULL "
        "WHERE id=?",
        (request_id, view.id),
    )
    return request_view(request_row(conn, request_id))


def _admit(
    conn: sqlite3.Connection,
    home: Path,
    view: DecisionView,
    now: float,
    policy: ReviewPolicy,
    budget: ReadBudget,
    proof: VerifiedDecision | None = None,
) -> RequestView:
    """Use one caller budget for proof and the exact owner-window admission."""
    approval = _approval(conn, view.id, view.revision)
    candidate = view.candidate
    if candidate is None:
        raise ValueError("decision_erased")
    existing = conn.execute(
        "SELECT * FROM review_request WHERE candidate_id=? AND "
        "decision_revision=?",
        (view.id, view.revision),
    ).fetchone()
    control = conn.execute("SELECT * FROM review_control").fetchone()
    if not control["admission"]:
        raise ValueError("learning_paused")
    preparation = conn.execute(
        "SELECT r.* FROM review_request r JOIN memory_candidate c "
        "ON c.request_id=r.id WHERE c.id=?",
        (view.id,),
    ).fetchone()
    if (
        preparation is None
        or preparation["policy_digest"] != digest(policy)
        or preparation["policy_json"] != canonical_json(policy)
    ):
        raise ValueError("rejected_validation")
    proof = proof or verify_decision(
        conn,
        home,
        DecisionIntent.model_validate_json(approval["locator_json"]),
        policy,
        budget,
    )
    if (
        proof.candidate_digest != view.digest
        or proof.candidate_id != view.id
        or proof.action != "approve"
        or proof.owner_evidence_id != approval["owner_evidence_id"]
        or proof.display_evidence_id != approval["display_evidence_id"]
    ):
        raise ValueError("rejected_fenced")
    for ref in candidate.targets:
        target(conn, ref, candidate.scope)
    source, _ = check_window(
        conn, proof.owner_window, candidate.scope, policy, budget=budget
    )
    if existing is None:
        return _insert_application(conn, view, proof, policy, source, now)
    inputs = {
        r[0]
        for r in conn.execute(
            "SELECT evidence_id FROM review_input WHERE request_id=?",
            (existing["id"],),
        )
    }
    if (
        existing["duplicate_key"]
        != _application_key(view.id, view.digest, view.revision)
        or approval["application_request_id"] != existing["id"]
        or inputs
        != {
            *candidate.original_evidence_ids,
            proof.owner_evidence_id,
            proof.display_evidence_id,
        }
        or request_view(existing).window != proof.owner_window
        or existing["scope_json"] != canonical_json(candidate.scope)
        or existing["policy_json"] != canonical_json(policy)
        or existing["policy_digest"] != digest(policy)
        or existing["session_root"] != source["session_root"]
    ):
        raise ValueError("rejected_fenced")
    return request_view(existing)


def admit_application(
    conn: sqlite3.Connection,
    home: Path,
    ident: DecisionId,
    revision: int,
    now: float,
) -> RequestView:
    """Admit one active approval using the caller's writer transaction.

    Args:
        conn: Caller-owned write connection and transaction.
        home: Private store home for current evidence checks.
        ident: Exact immutable candidate identity.
        revision: Positive current accepted approval revision.
        now: Finite nonnegative timestamp.

    Returns:
        The unique application request, with no execution attempt or receipt.

    Raises:
        ValueError: If approval, current policy or evidence is unavailable.
    """
    require_transaction(conn, now)
    policy = _policy(conn)
    _approval(conn, ident, revision)
    return _admit(
        conn,
        home,
        decision_view(conn, ident),
        now,
        policy,
        ReadBudget(policy.max_input_bytes),
    )


def _code(exc: ValueError) -> DecisionCode:
    """Expose only known refusal codes and preserve precise budget causes."""
    value = exc.code if isinstance(exc, EvidenceLimitError) else str(exc)
    value = EVIDENCE_ERRORS.get(value, value)
    try:
        return TypeAdapter(DecisionCode).validate_python(value)
    except ValueError:
        return "decision_invalid"


def _verify_pending(
    conn: sqlite3.Connection,
    home: Path,
    ident: DecisionId,
    now: float,
    policy: ReviewPolicy,
    budget: ReadBudget,
) -> VerifiedDecision | None:
    """Roll back failed proof writes before retaining only finite status."""
    row = conn.execute(
        "SELECT * FROM memory_decision WHERE candidate_id=? "
        "AND accepted_revision IS NULL AND state='waiting_verification' "
        "ORDER BY submitted_at DESC,id DESC LIMIT 1",
        (ident,),
    ).fetchone()
    if row is None:
        return None
    conn.execute("SAVEPOINT memory_proof")
    try:
        intent = DecisionIntent.model_validate_json(row["locator_json"])
        proof = verify_decision(conn, home, intent, policy, budget)
        accept_decision(conn, proof, now, policy=policy)
        return proof
    except ValueError as exc:
        conn.execute("ROLLBACK TO memory_proof")
        code = _code(exc)
        conn.execute(
            "UPDATE memory_decision SET state=?,code=? WHERE id=?",
            (
                (
                    "waiting_verification"
                    if code == "waiting_index"
                    else "blocked"
                ),
                code,
                row["id"],
            ),
        )
    finally:
        conn.execute("RELEASE memory_proof")


def _saved(
    conn: sqlite3.Connection, view: DecisionView, now: float
) -> DecisionView:
    """Return the receipt and retire losing intent without acceptance."""
    if view.receipt is None:
        raise ValueError("decision_invalid")
    row = conn.execute(
        "SELECT locator_json FROM memory_decision WHERE candidate_id=? "
        "AND accepted_revision IS NULL AND state='waiting_verification' "
        "ORDER BY submitted_at DESC,id DESC LIMIT 1",
        (view.id,),
    ).fetchone()
    if row is not None:
        return submit_intent(
            conn, DecisionIntent.model_validate_json(row[0]), now
        )
    return view


def _step(
    conn: sqlite3.Connection,
    home: Path,
    ident: DecisionId,
    now: float,
    policy: ReviewPolicy,
    budget: ReadBudget,
) -> DecisionView:
    """Perform one targeted proof and admission pass without execution."""
    view = decision_view(conn, ident)
    if view.state == "saved":
        return _saved(conn, view, now)
    if view.state in {"rejected", "superseded", "erased"}:
        return view
    proof = _verify_pending(conn, home, ident, now, policy, budget)
    view = decision_view(conn, ident)
    if view.state != "approved_pending":
        return view
    conn.execute("SAVEPOINT memory_application")
    try:
        _admit(conn, home, view, now, policy, budget, proof)
        conn.execute(
            "UPDATE memory_candidate SET code=NULL WHERE id=?", (ident,)
        )
    except ValueError as exc:
        conn.execute("ROLLBACK TO memory_application")
        conn.execute(
            "UPDATE memory_candidate SET code=? WHERE id=?",
            (_code(exc), ident),
        )
    finally:
        conn.execute("RELEASE memory_application")
    return decision_view(conn, ident)


def request_application(
    home: Path, ident: DecisionId, now: float
) -> DecisionView:
    """Check only the requested candidate under one short writer root."""
    with database(home) as conn:
        require_transaction(conn, now)
        view = decision_view(conn, ident)
        if view.state == "saved":
            return _saved(conn, view, now)
        policy = _policy(conn)
        return _step(
            conn, home, ident, now, policy, ReadBudget(policy.max_input_bytes)
        )


def continue_decisions(
    home: Path,
    generation: int,
    now: float,
    *,
    limit: int = 1,
) -> tuple[DecisionView, ...]:
    """Continue bounded deterministic work under the existing owner's fence."""
    with database(home) as conn:
        require_transaction(conn, now)
        control = conn.execute("SELECT * FROM review_control").fetchone()
        if (
            type(generation) is not int
            or generation < 1
            or generation != control["generation"]
        ):
            raise ValueError("rejected_fenced")
        policy = _policy(conn)
        if type(limit) is not int or not 1 <= limit <= policy.concurrency:
            raise ValueError("decision_invalid")
        if not control["admission"]:
            return ()
        rows = conn.execute(
            "SELECT c.id FROM memory_candidate c WHERE "
            "c.state IN "
            "('waiting_verification','blocked','approved_pending','saved') "
            "AND (EXISTS(SELECT 1 FROM memory_decision d WHERE "
            "d.candidate_id=c.id "
            "AND d.accepted_revision IS NULL AND "
            "d.state='waiting_verification') "
            "OR (c.state='approved_pending' AND c.application_request_id IS "
            "NULL)) "
            "ORDER BY c.rowid LIMIT ?",
            (limit,),
        ).fetchall()
        budget = ReadBudget(policy.max_input_bytes)
        output: list[DecisionView] = []
        for row in rows:
            output.append(_step(conn, home, row[0], now, policy, budget))
            # The TEXT primary key alone identifies decisions. Mutable rowid
            # yields advisory queue order across restarts, never authority.
            # Maintenance may reset this order without changing permission.
            conn.execute(
                "UPDATE memory_candidate SET rowid="
                "(SELECT max(rowid)+1 FROM memory_candidate) WHERE id=?",
                (row[0],),
            )
        return tuple(output)
