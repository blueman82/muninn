"""Reconcile one contained review outcome before releasing its worker."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from muninn import review_prompt
from muninn.review_commit import CommitContext, commit_review
from muninn.review_models import ReviewContext
from muninn.review_process import ProcessOutcome
from muninn.review_round_state import Round
from muninn.review_rounds import FinalRequest, execute_read, parse_round
from muninn.review_supervisor_store import database


def execute(
    home: Path,
    pending: Round,
    outcome: ProcessOutcome,
    now: float,
    fail: Callable[[ReviewContext, str, float], None],
) -> bool:
    """Commit a final result or retain a checked read for another round."""
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
            )
            return False
        context = review_prompt.refresh(home, pending.context)
        request = parse_round(outcome.stdout, context)
        if isinstance(request, FinalRequest):
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
                    request.result,
                )
            return False
        result = execute_read(home, request, context)
        pending.context = review_prompt.refresh(home, context).model_copy(
            update={"round_index": context.round_index + 1}
        )
        pending.history.append(
            (request.model_dump(mode="json"), json.loads(result))
        )
        pending.spawned = False
        pending.handle = None
        pending.launch = None
        return True
    except ValueError as exc:
        code = (
            str(exc)
            if str(exc).startswith("rejected_")
            else "rejected_validation"
        )
        fail(pending.context, code, now)

    return False
