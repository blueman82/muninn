"""Bind task-only inputs, historical transport context and paired results."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import Field

from muninn import store
from muninn.knowledge_models import (
    AttemptId,
    Digest,
    RequestId,
    StrictModel,
    Text,
)
from muninn.review_budget import call_view
from muninn.review_evidence_models import NonnegativeInt, PositiveInt
from muninn.review_models import (
    ObservedConfig,
    ReviewContext,
    ReviewPolicy,
    Usage,
)
from muninn.review_request_views import attempt_view, request_row, request_view
from muninn.review_runner import read_private_file
from tools.learning_eval.cases import digest, strict_json
from tools.learning_eval.native_codex import native_prompt
from tools.learning_eval.pilot_evidence import ExecutionResult, SavedOutput
from tools.learning_eval.pilot_models import (
    PilotManifest,
    PilotResult,
    PilotTrial,
)
from tools.learning_eval.task_protocol import (
    parse_answer,
    task_prompt,
    task_schema,
)
from tools.learning_eval.trial_models import AccountCall, ScheduledCall


class ArtifactRef(StrictModel):
    """Select an exact bounded private source file."""

    path: Path
    sha256: Digest

    def read(self) -> bytes:
        """Refuse drift before interpreting a private JSON artifact."""
        raw = read_private_file(self.path, 4 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("task_only_source_changed")
        strict_json(raw)
        return raw


class HistoricalMapping(StrictModel):
    """Read the existing learner's identity without issuing new authority."""

    home: Path
    schedule: ScheduledCall
    request_id: RequestId
    attempt_id: AttemptId
    round_index: PositiveInt
    generation: PositiveInt


class SourceBinding(StrictModel):
    """Bind one present-arm exposure and the original transport records."""

    case_id: Text
    source_trial_id: Text
    learned_digest: Digest
    exposure_digest: Digest
    learner_output: ArtifactRef
    mapping: ArtifactRef
    records_digest: Digest


class TaskOnlyManifest(StrictModel):
    """Freeze one six-call supplement against an existing spent Account."""

    schema_version: Literal[1] = 1
    kind: Literal["native-task-only-same-state-v1"] = (
        "native-task-only-same-state-v1"
    )
    pilot_manifest: ArtifactRef
    pilot_result: ArtifactRef
    import_receipt: ArtifactRef
    account_preimage: Digest
    baseline_calls: tuple[AccountCall, ...]
    sources: tuple[SourceBinding, ...]
    artifact_path: Path


class TaskOnlyPlan(StrictModel):
    """Declare each arm before spending its unique task-only claim."""

    source_trial_id: Text
    case_id: Text
    learned_digest: Digest
    exposure_digest: Digest
    prompt_digest: Digest
    schedule: ScheduledCall


class TaskOnlyRow(StrictModel):
    """Keep every measured, refused or stopped supplemental row."""

    plan: TaskOnlyPlan
    outcome: Text
    answer: str | None = None
    output_path: Path | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    observed: ObservedConfig | None = None
    usage: Usage | None = None

    @classmethod
    def from_execution(
        cls,
        plan: TaskOnlyPlan,
        execution: ExecutionResult,
        limit: int,
    ) -> TaskOnlyRow:
        """Retain observed metadata when domain answer validation fails."""
        outcome = execution.outcome
        if outcome is None:
            return cls(plan=plan, outcome="unavailable_evidence")
        code, answer = outcome.error_code or "completed", None
        if outcome.error_code is None:
            try:
                answer = parse_answer(outcome.stdout, limit).answer
            except ValueError as exc:
                code = str(exc)
        return cls(
            plan=plan,
            outcome=code,
            answer=answer,
            output_path=execution.evidence_path,
            elapsed_seconds=outcome.elapsed_seconds,
            observed=outcome.observed,
            usage=outcome.usage,
        )


class TaskOnlyResult(StrictModel):
    """Report tuning observations separately from all scored pilot rows."""

    manifest_digest: Digest
    source_result_digest: Digest
    rows: tuple[TaskOnlyRow, ...]
    incremental_cli_launches: NonnegativeInt
    incremental_reserved_cost_decimal: Text
    cumulative_cli_launches: NonnegativeInt
    cumulative_reserved_cost_decimal: Text
    actual_model_requests: None = None
    actual_cost_decimal: None = None
    status: Literal["unverified"] = "unverified"
    limitations: tuple[Text, ...] = (
        "same_state_task_only_tuning_not_independent_quality_evidence",
        "recorded_paraphrase_had_fts_hits_no_fts_miss_claim",
        "native_capture_and_production_qualification_unverified",
        "internal_requests_and_actual_spend_unknown",
    )


def reference(path: Path) -> ArtifactRef:
    """Capture exact private bytes without reading outside the chosen path."""
    return ArtifactRef(
        path=path,
        sha256=hashlib.sha256(
            read_private_file(path, 4 * 1024 * 1024)
        ).hexdigest(),
    )


def source_binding(
    pilot: PilotManifest, trial: PilotTrial, calls: tuple[AccountCall, ...]
) -> tuple[SourceBinding, ReviewContext]:
    """Reconstruct terminal learner transport bounds entirely read-only."""
    selected = [
        c
        for c in calls
        if c.schedule.trial_id == trial.id and c.schedule.purpose == "learning"
    ]
    if (
        not selected
        or trial.exposure is None
        or trial.comparator != "new_present"
        or trial.outcome != "completed"
    ):
        raise ValueError("task_only_source_binding")
    last = max(selected, key=lambda c: c.schedule.round_index)
    output = reference(pilot.artifact_path / (last.schedule.id + ".json"))
    saved = SavedOutput.model_validate_json(output.read())
    mapping_ref = reference(
        pilot.artifact_path / (last.schedule.id + "-mapping.json")
    )
    mapping = HistoricalMapping.model_validate_json(mapping_ref.read())
    if (
        output.path not in trial.output_paths
        or output.sha256 != last.evidence_digest
        or saved.schedule != last.schedule
        or saved.error_code is not None
        or saved.usage != last.usage
        or not last.cleaned
        or last.state != "completed"
        or mapping.home != trial.store_home
        or mapping.schedule != last.schedule
        or mapping.round_index != last.schedule.round_index
        or digest(mapping.model_dump(mode="json"))
        != saved.learner_mapping_digest
    ):
        raise ValueError("task_only_mapping_binding")
    context, records = historical_context(pilot, mapping, saved)
    return (
        SourceBinding(
            case_id=trial.case_id,
            source_trial_id=trial.id,
            learned_digest=trial.exposure.learned_digest,
            exposure_digest=digest(trial.exposure.model_dump(mode="json")),
            learner_output=output,
            mapping=mapping_ref,
            records_digest=records,
        ),
        context,
    )


def historical_context(
    pilot: PilotManifest, mapping: HistoricalMapping, saved: SavedOutput
) -> tuple[ReviewContext, str]:
    """Verify persisted request, attempt, policy and completed learner call."""
    policy = pilot.assessment.policies[0]
    with closing(store.connect_ro(store.db_path(mapping.home))) as conn:
        row = request_row(conn, mapping.request_id)
        request = request_view(row)
        attempt = attempt_view(conn, mapping.attempt_id)
        call_row = conn.execute(
            "SELECT * FROM review_call WHERE attempt_id=? AND round_index=?",
            (mapping.attempt_id, mapping.round_index),
        ).fetchone()
        if call_row is None:
            raise ValueError("task_only_mapping_binding")
        call = call_view(call_row)
        evidence = tuple(
            r[0]
            for r in conn.execute(
                "SELECT evidence_id FROM review_input WHERE request_id=? "
                "ORDER BY evidence_id",
                (request.id,),
            )
        )
        if (
            request.current_attempt_id != attempt.id
            or request.state != "completed"
            or request.policy_digest != digest(policy.model_dump(mode="json"))
            or ReviewPolicy.model_validate_json(row["policy_json"]) != policy
            or attempt.request_id != request.id
            or attempt.generation != mapping.generation
            or attempt.ended_at is None
            or attempt.outcome not in {"completed", "no_change"}
            or attempt.requested != policy.runner
            or call.requested != policy.runner
            or call.state != "completed"
            or call.outcome != "completed"
            or call.usage != saved.usage
            or call.observed != saved.observed
            or call.policy_digest != mapping.schedule.policy_digest
            or call.budget_account_id != pilot.assessment.budget.account_id
        ):
            raise ValueError("task_only_mapping_binding")
        records = digest(
            {
                "request": dict(row),
                "attempt": attempt.model_dump(mode="json"),
                "call": call.model_dump(mode="json"),
                "evidence": evidence,
            }
        )
    return (
        ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=attempt.generation,
            scope=request.scope,
            policy=policy,
            admitted_windows=(request.window,),
            evidence_ids=evidence,
            round_index=1,
            deadline=time.monotonic() + policy.deadline_seconds,
        ),
        records,
    )


def prompt_for(
    pilot: PilotManifest, trial: PilotTrial, comparator: str
) -> bytes:
    """Vary only supplied knowledge between the same-source task prompts."""
    if trial.exposure is None:
        raise ValueError("task_only_source_binding")
    return task_prompt(
        pilot.task_instructions,
        trial.exposure.query,
        trial.exposure.prompt_context if comparator == "new_present" else {},
        pilot.assessment.policies[0].max_input_bytes,
    )


def plans_for(
    manifest: TaskOnlyManifest, pilot: PilotManifest, result: PilotResult
) -> tuple[TaskOnlyPlan, ...]:
    """Derive exactly six task slots from the immutable source result."""
    plans: list[TaskOnlyPlan] = []
    policy = pilot.assessment.policies[0]
    for source in manifest.sources:
        trial = next(
            t for t in result.trials if t.id == source.source_trial_id
        )
        for comparator in ("new_present", "new_withheld"):
            ident = "T_" + digest(
                [
                    manifest.kind,
                    manifest.pilot_result.sha256,
                    trial.id,
                    comparator,
                ]
            )
            schedule = ScheduledCall.model_validate_json(
                json.dumps(
                    {
                        "id": "A_" + digest([ident, "task", 1, 1])[:32],
                        "trial_id": ident,
                        "assessment_id": pilot.assessment.id,
                        "comparator": comparator,
                        "purpose": "task",
                        "protocol": "muninn-task-answer-v1",
                        "schema_digest": digest(task_schema()),
                        "retry_index": 1,
                        "round_index": 1,
                        "policy_digest": digest(
                            policy.model_dump(mode="json")
                        ),
                        "config_digest": pilot.assessment.config_digest,
                        "authorization_digest": digest(
                            pilot.task[comparator].authorization.model_dump(
                                mode="json"
                            )
                        ),
                    }
                )
            )
            actual = native_prompt(
                prompt_for(pilot, trial, comparator),
                schedule.protocol,
                policy.max_input_bytes,
            )
            plans.append(
                TaskOnlyPlan(
                    source_trial_id=trial.id,
                    case_id=trial.case_id,
                    learned_digest=source.learned_digest,
                    exposure_digest=source.exposure_digest,
                    prompt_digest=hashlib.sha256(actual).hexdigest(),
                    schedule=schedule,
                )
            )
    return tuple(plans)
