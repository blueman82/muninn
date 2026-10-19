"""Drive public checked learning with serialized accounted synthetic calls."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from muninn import review_prompt
from muninn.review_commit import CommitContext, commit_review
from muninn.review_models import (
    Receipt,
    RequestView,
    ReviewContext,
    ReviewPolicy,
)
from muninn.review_request_views import request_row, request_view
from muninn.review_requests import (
    claim_or_refuse_attempt,
    fail_attempt,
    retry_request,
)
from muninn.review_rounds import FinalRequest, execute_read, parse_round
from muninn.review_supervisor_store import database
from tools.learning_eval.account import Account
from tools.learning_eval.execution import ExecutionEvidence, execute_result
from tools.learning_eval.pilot_bridge import LearnerBridge
from tools.learning_eval.pilot_models import PilotManifest, PlannedTrial


@dataclass(frozen=True)
class LearningResult:
    """Return actual receipts and the last real context as transport bounds."""

    receipts: tuple[Receipt, ...]
    output_paths: tuple[Path, ...]
    call_ids: tuple[str, ...]
    context: ReviewContext


def claim(home: Path, request_id: str, generation: int) -> ReviewContext:
    """Claim ordinary review admission under the currently owned generation."""
    with database(home) as conn:
        row = request_row(conn, request_id)
        view = request_view(row)
        policy = ReviewPolicy.model_validate_json(row["policy_json"])
        attempt = claim_or_refuse_attempt(
            conn, request_id, generation, time.time()
        )
        if isinstance(attempt, RequestView):
            raise ValueError("request_unavailable")
    return ReviewContext(
        request_id=view.id,
        attempt_id=attempt.id,
        generation=generation,
        scope=view.scope,
        policy=policy,
        admitted_windows=(view.window,),
        evidence_ids=(),
        round_index=1,
        deadline=time.monotonic() + policy.deadline_seconds,
    )


def learn(
    manifest: PilotManifest,
    trial: PlannedTrial,
    account: Account,
    request_id: str,
    generation: int,
    artifact: bytes,
) -> LearningResult:
    """Retry only through public claims, retaining each separate spent call."""
    paths: list[Path] = []
    calls: list[str] = []
    policy = manifest.assessment.policies[0]
    for retry in range(1, policy.max_attempts + 1):
        context = claim(trial.store_home, request_id, generation)
        try:
            return _rounds(
                manifest,
                trial,
                account,
                context,
                retry,
                artifact,
                (paths, calls),
            )
        except ValueError as exc:
            code = str(exc)
            transient = code in {"deadline", "unavailable"}
            with database(trial.store_home) as conn:
                try:
                    fail_attempt(
                        conn,
                        context.attempt_id,
                        code if transient else "rejected_validation",
                        time.time(),
                    )
                except ValueError as failed:
                    if str(failed) != "rejected_fenced":
                        raise
            if not transient or retry == policy.max_attempts:
                raise
            with database(trial.store_home) as conn:
                retry_request(conn, request_id, time.time())
    raise ValueError("review_attempt_limit")


def _rounds(
    manifest: PilotManifest,
    trial: PlannedTrial,
    account: Account,
    context: ReviewContext,
    retry: int,
    artifact: bytes,
    accumulated: tuple[list[Path], list[str]],
) -> LearningResult:
    """Preserve admission history and the original deadline."""
    paths, calls = accumulated
    home = trial.store_home
    context, initial, knowledge = review_prompt.initialize(home, context)
    history: list[tuple[object, object]] = []
    binding = manifest.learning[trial.comparator]
    while context.round_index <= context.policy.max_rounds:
        context = review_prompt.refresh(home, context)
        prompt = review_prompt.assemble(
            artifact, context, initial, knowledge, history
        )
        schedule = next(
            s
            for s in trial.calls
            if s.purpose == "learning"
            and s.retry_index == retry
            and s.round_index == context.round_index
        )
        result = execute_result(
            account,
            context,
            binding.capability,
            prompt,
            binding.authorization,
            schedule,
            evidence=ExecutionEvidence(
                manifest.artifact_path,
                LearnerBridge(home, context, schedule, manifest.artifact_path),
            ),
        )
        calls.append(schedule.id)
        if result.evidence_path is not None:
            paths.append(result.evidence_path)
        outcome = result.outcome
        if outcome is None:
            raise ValueError("unavailable_evidence")
        if outcome.error_code is not None:
            raise ValueError(
                "deadline"
                if outcome.error_code == "deadline"
                else "unavailable"
            )
        context = review_prompt.refresh(home, context)
        operation = parse_round(outcome.stdout, context)
        if isinstance(operation, FinalRequest):
            with database(home, transaction=False) as conn:
                receipt = commit_review(
                    conn,
                    CommitContext(
                        home=home,
                        request_id=context.request_id,
                        attempt_id=context.attempt_id,
                        generation=context.generation,
                        evidence_ids=context.evidence_ids,
                        now=time.time(),
                    ),
                    operation.result,
                )
            return LearningResult(
                (receipt,), tuple(paths), tuple(calls), context
            )
        response = execute_read(home, operation, context)
        history.append(
            (operation.model_dump(mode="json"), json.loads(response))
        )
        context = review_prompt.refresh(home, context).model_copy(
            update={"round_index": context.round_index + 1}
        )
    raise ValueError("budget_round_limit")
