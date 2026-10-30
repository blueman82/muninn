"""Reconcile one contained review outcome before releasing its worker."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from muninn import review_prompt
from muninn.knowledge_models import ReviewResult
from muninn.review_candidates import CommitRejectedError, ProposalRejectedError
from muninn.review_commit import CommitContext, commit_review
from muninn.review_correction import (
    CorrectionFeedback,
    CorrectionState,
    RoundFormatError,
    require_reduction,
)
from muninn.review_diagnostics import FailurePhase
from muninn.review_models import ReviewContext
from muninn.review_process import ProcessOutcome
from muninn.review_read_budget import EVIDENCE_LIMIT_CODES, EvidenceLimitError
from muninn.review_round_state import Round
from muninn.review_rounds import FinalRequest, execute_read, parse_round
from muninn.review_supervisor_store import database, mark_correction


def failure_code(error: ValueError | OSError, fallback: str) -> str:
    """Keep known verification causes without exposing exception text."""
    cause = error.code if isinstance(error, EvidenceLimitError) else str(error)
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
        }
        or cause in EVIDENCE_LIMIT_CODES
        else fallback
    )


def _advance(pending: Round, context: ReviewContext) -> None:
    """Keep the permit and deadline while clearing finished launch data."""
    pending.context = context.model_copy(
        update={"round_index": context.round_index + 1}
    )
    pending.spawned = False
    pending.handle = None
    pending.launch = None
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
    outcome: ProcessOutcome,
    now: float,
    fail: Callable[[ReviewContext, str, float, str], None],
) -> bool:
    """Commit a final result or retain one checked read or correction round."""
    stage: FailurePhase = "native output"
    original = None
    try:
        try:
            if outcome.error_code is not None:
                fail(
                    pending.context,
                    (
                        "deadline"
                        if outcome.error_code == "deadline"
                        else (
                            "rejected_validation"
                            if outcome.error_code
                            in {"invalid_native_output", "output_limit"}
                            else "unavailable"
                        )
                    ),
                    now,
                    stage,
                )
                return False
            stage = "original evidence"
            pending.context = review_prompt.refresh(home, pending.context)
            stage = "model format"
            request = parse_round(outcome.stdout, pending.context)
            if pending.correction is not None:
                stage = "candidate validation"
                if not isinstance(request, FinalRequest):
                    raise ValueError("rejected_validation")
                if pending.correction.original is not None:
                    require_reduction(
                        pending.correction.original, request.result
                    )
            if isinstance(request, FinalRequest):
                stage = "commit"
                original = request.result
                _commit(home, pending, original, now)
                return False
            stage = "controlled read"
            result = execute_read(home, request, pending.context)
            stage = "original evidence"
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
            stage = "commit"
            _correct(home, pending, exc, original)
            return True
    except ValueError as exc:
        fail(
            pending.context,
            failure_code(exc, "rejected_validation"),
            now,
            exc.phase if isinstance(exc, CommitRejectedError) else stage,
        )
    return False
