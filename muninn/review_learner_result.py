"""Reconcile one finished learning call and plan the next round."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from muninn import review_prompt
from muninn.knowledge_models import ReviewResult
from muninn.review_candidates import ProposalRejectedError
from muninn.review_commit import CommitContext, commit_review
from muninn.review_correction import (
    CorrectionFeedback,
    CorrectionState,
    RoundFormatError,
    require_reduction,
)
from muninn.review_db import database
from muninn.review_learner_round import Round
from muninn.review_models import ReviewContext
from muninn.review_progress import digest
from muninn.review_read_budget import EVIDENCE_LIMIT_CODES, EvidenceLimitError
from muninn.review_recovery_codes import RUNTIME_CODES
from muninn.review_rounds import FinalRequest, execute_read, parse_round
from muninn.review_runtime_bridge import effective_policy


class Outcome(Protocol):
    """The part of a finished call that the result check reads."""

    @property
    def stdout(self) -> bytes:
        """Decoded result bytes."""
        ...

    @property
    def error_code(self) -> str | None:
        """Failure code, or None on success."""
        ...


def failure_code(error: ValueError | OSError, fallback: str) -> str:
    """Keep known verification causes without exposing exception text."""
    cause = error.code if isinstance(error, EvidenceLimitError) else str(error)
    if cause in {"budget_deadline", "deadline"}:
        # The round clock is the same time limit as the process clock.
        return "deadline"
    return (
        cause
        if cause
        in {
            "rejected_authority",
            "rejected_evidence",
            "rejected_erased",
            "rejected_fenced",
            "rejected_revision",
            "rejected_scope",
            "rejected_validation",
            "prompt_limit",
            "source_unavailable",
            "review_policy_required",
            "incomplete_window",
            "budget_clock_reversed",
        }
        or cause in EVIDENCE_LIMIT_CODES
        or cause in RUNTIME_CODES
        else fallback
    )


def _advance(pending: Round, context: ReviewContext) -> None:
    """Keep the permit and deadline and clear the finished prompt."""
    pending.context = context.model_copy(
        update={"round_index": context.round_index + 1}
    )
    pending.prompt = b""


def _correct(
    home: Path,
    pending: Round,
    error: RoundFormatError | ProposalRejectedError,
    original: ReviewResult | None,
) -> None:
    """Request one correction only after the failed writer root has closed."""
    context = review_prompt.refresh(home, pending.context)
    feedback = (
        error.feedback
        if isinstance(error, ProposalRejectedError)
        else CorrectionFeedback(
            kind="format", code="model_format", collection=None, index=None
        )
    )
    with database(home) as conn:
        mark_correction(conn, context, feedback.kind)
    pending.correction = CorrectionState(feedback, original)
    _advance(pending, context)


def _commit(
    home: Path, pending: Round, result: ReviewResult, now: float
) -> None:
    """Release the writer root before a deferred refusal escapes."""
    context = pending.context
    with database(home, transaction=False) as conn:
        commit_review(
            conn,
            CommitContext(
                home=home,
                request_id=context.request_id,
                attempt_id=context.attempt_id,
                generation=context.generation,
                evidence_ids=context.evidence_ids,
                now=now,
                knowledge_digest=context.knowledge_digest,
            ),
            result,
            defer_candidate_failure=(
                pending.correction is None
                and context.round_index < context.policy.max_rounds
            ),
        )


def execute(
    home: Path,
    pending: Round,
    outcome: Outcome,
    now: float,
    fail: Callable[[ReviewContext, str, float], None],
) -> bool:
    """Commit a final result or retain one checked read or correction round."""
    original = None
    try:
        try:
            if outcome.error_code is not None:
                fail(
                    pending.context,
                    (
                        outcome.error_code
                        if outcome.error_code in RUNTIME_CODES
                        else (
                            "deadline"
                            if outcome.error_code == "deadline"
                            else (
                                "rejected_validation"
                                if outcome.error_code
                                in {"invalid_native_output", "output_limit"}
                                else "unavailable"
                            )
                        )
                    ),
                    now,
                )
                return False
            pending.context = review_prompt.refresh(home, pending.context)
            request = parse_round(outcome.stdout, pending.context)
            if pending.correction is not None:
                if not isinstance(request, FinalRequest):
                    raise ValueError("rejected_validation")
                if pending.correction.original is not None:
                    require_reduction(
                        pending.correction.original, request.result
                    )
            if isinstance(request, FinalRequest):
                original = request.result
                _commit(home, pending, original, now)
                return False
            result = execute_read(home, request, pending.context)
            context = review_prompt.refresh(home, pending.context)
            pending.history.append(
                (request.model_dump(mode="json"), json.loads(result))
            )
            _advance(pending, context)
            return True
        except (RoundFormatError, ProposalRejectedError) as exc:
            if (
                pending.correction is not None
                or pending.context.round_index
                >= pending.context.policy.max_rounds
            ):
                raise
            _correct(home, pending, exc, original)
            return True
    except ValueError as exc:
        fail(pending.context, failure_code(exc, "rejected_validation"), now)
    return False


def mark_correction(
    conn: sqlite3.Connection,
    context: ReviewContext,
    kind: Literal["format", "reduction"],
) -> None:
    """Mark one completed call under the caller's fenced transaction."""
    if not conn.in_transaction:
        raise ValueError("transaction_required")
    row = conn.execute(
        "SELECT * FROM review_request WHERE id=?", (context.request_id,)
    ).fetchone()
    try:
        if row is None or effective_policy(conn, row) != context.policy:
            raise ValueError("rejected_fenced")
    except (ValueError, OSError):
        raise ValueError("rejected_fenced") from None
    policy = row["policy_json"]
    changed = conn.execute(
        "UPDATE review_call SET outcome=? WHERE attempt_id=? "
        "AND round_index=? AND state='completed' AND ended_at IS NOT NULL "
        "AND policy_digest=? AND EXISTS ("
        "SELECT 1 FROM review_request r JOIN review_attempt a "
        "ON a.id=r.current_attempt_id CROSS JOIN review_control c "
        "WHERE r.id=? AND a.id=review_call.attempt_id AND a.request_id=r.id "
        "AND r.state='running' AND a.ended_at IS NULL "
        "AND a.generation=? AND c.generation=a.generation "
        "AND c.admission=1 AND r.policy_digest=? "
        "AND c.policy_digest=? "
        "AND r.policy_json=?)",
        (
            f"correction_{kind}_requested",
            context.attempt_id,
            context.round_index,
            digest(context.policy),
            context.request_id,
            context.generation,
            row["policy_digest"],
            digest(context.policy),
            policy,
        ),
    ).rowcount
    if changed != 1:
        raise ValueError("rejected_fenced")
