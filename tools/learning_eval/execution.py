"""Run one synthetic harness call through the shared concrete containment."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from muninn import review_prompt
from muninn.review_models import ReviewContext
from muninn.review_process import (
    ProcessMonitor,
    ProcessOutcome,
    WorkerLaunched,
    WorkerReady,
    cancel_registered,
)
from muninn.review_process_protocol import activation
from muninn.review_runner import RunnerCapability
from muninn.review_spawn import create_wrapper
from tools.learning_eval.account import Account
from tools.learning_eval.capability import prepare_capability_trial
from tools.learning_eval.cases import digest
from tools.learning_eval.models import TrialAuthorization
from tools.learning_eval.native_codex import execution_terms, native_prompt
from tools.learning_eval.native_terms import assessment_deadline
from tools.learning_eval.pilot_bridge import LearnerBridge
from tools.learning_eval.pilot_evidence import (
    ExecutionResult,
    restore_output,
    saved_output,
)
from tools.learning_eval.task_protocol import protocol_schema
from tools.learning_eval.trial_models import AccountCall, ScheduledCall


@dataclass(frozen=True)
class ExecutionEvidence:
    """Select private output storage and an optional real learner mapping."""

    root: Path
    learner: LearnerBridge | None = None


def validate_execution(
    account: Account,
    context: ReviewContext,
    authorization: TrialAuthorization,
    schedule: ScheduledCall,
) -> None:
    """Refuse unsupported adapters and bind the exact per-model-call policy."""
    account.validate(schedule, authorization)
    if schedule.comparator not in {"new_present", "new_withheld"}:
        raise ValueError("comparator_unsupported")
    observed = context.policy.runner.model_dump(
        mode="json", exclude={"executable"}
    )
    if (
        (
            schedule.schema_digest is not None
            and schedule.schema_digest
            != digest(protocol_schema(schedule.protocol))
        )
        or context.round_index != schedule.round_index
        or digest(context.policy.model_dump(mode="json"))
        != schedule.policy_digest
        or account.assessment.comparator_config_digests[schedule.comparator]
        != digest(observed)
    ):
        raise ValueError("call_binding_mismatch")


def execute_result(
    account: Account,
    context: ReviewContext,
    capability: RunnerCapability,
    prompt: bytes,
    authorization: TrialAuthorization,
    schedule: ScheduledCall,
    *,
    evidence: ExecutionEvidence | None = None,
) -> ExecutionResult:
    """Spend one durable claim before creating the shared idle wrapper."""
    artifacts = evidence.root if evidence is not None else None
    learner = evidence.learner if evidence is not None else None
    try:
        if capability.runner == "codex_cli":
            prompt = native_prompt(
                prompt, schedule.protocol, context.policy.max_input_bytes
            )
        validate_execution(account, context, authorization, schedule)
        prepare_capability_trial(
            context, capability, prompt, authorization, schedule.protocol
        )
        terms = execution_terms(
            account, capability, schedule.id, authorization.candidate_digest
        )
    except (ValueError, OSError) as exc:
        code = (
            "comparator_unsupported"
            if str(exc) == "comparator_unsupported"
            else "configuration_refused"
        )
        account.refuse(schedule, authorization, code)
        raise
    call, permit = account.claim(schedule, authorization, time.time())
    mapping = learner.mapping_digest() if learner is not None else None
    limit = context.policy.max_output_bytes * 8 + 65536
    if not permit:
        return (
            restore_output(artifacts, call, prompt, mapping, limit)
            if artifacts is not None
            else ExecutionResult(call, None, None)
        )
    if learner is not None:
        if learner.schedule != schedule or learner.context != context:
            raise ValueError("call_binding_mismatch")
        learner.reserve()
    remaining = min(
        account.remaining_wall(time.time()), context.policy.deadline_seconds
    )
    if terms is not None:
        remaining = min(
            remaining,
            assessment_deadline(terms, account.assessment) - time.time(),
        )
    context = context.model_copy(
        update={
            "deadline": min(context.deadline, time.monotonic() + remaining),
        }
    )
    monitor = ProcessMonitor(
        context.policy, runner=context.policy.runner.runner
    )
    try:
        fresh = prepare_capability_trial(
            context, capability, prompt, authorization, schedule.protocol
        )
        create_wrapper(
            fresh,
            monitor,
            schedule.id,
            schedule.round_index,
            context.policy.max_output_bytes,
        )
        outcome = _wait(
            account,
            monitor,
            context,
            capability,
            prompt,
            authorization,
            (schedule, learner),
        )
    finally:
        # The independent monitor retains owned children if persistence fails.
        monitor.stop_all()
    account.finish(schedule.id, outcome.usage, outcome.error_code, time.time())
    call = next(c for c in account.calls() if c.schedule.id == schedule.id)
    if artifacts is not None:
        return saved_output(
            artifacts, account, call, prompt, outcome, mapping, limit
        )
    return ExecutionResult(call, outcome, None)


def _wait(
    account: Account,
    monitor: ProcessMonitor,
    context: ReviewContext,
    capability: RunnerCapability,
    prompt: bytes,
    authorization: TrialAuthorization,
    binding: tuple[ScheduledCall, LearnerBridge | None],
) -> ProcessOutcome:
    """Persist readiness, freshly revalidate, then spend activation once."""
    schedule, learner = binding
    while True:
        for event in monitor.drain():
            if (event.attempt_id, event.round_index) != (
                schedule.id,
                schedule.round_index,
            ):
                raise ValueError("call_binding_mismatch")
            if isinstance(event, WorkerReady):
                try:
                    account.ready(schedule.id, event.identity, time.time())
                    if learner is not None:
                        learner.persist(event)
                    frame = _activation_frame(
                        account,
                        context,
                        capability,
                        prompt,
                        authorization,
                        binding,
                    )
                    account.activate(schedule.id, time.time())
                    monitor.activate(schedule.id, schedule.round_index, frame)
                except (ValueError, OSError):
                    cancel_registered(
                        monitor,
                        schedule.id,
                        schedule.round_index,
                        "activation_refused",
                    )
            elif isinstance(event, WorkerLaunched):
                if learner is not None:
                    learner.persist(event)
                account.launched(schedule.id, time.time())
            else:
                if learner is not None:
                    learner.persist(event)
                return event
        time.sleep(0.01)


def _activation_frame(
    account: Account,
    context: ReviewContext,
    capability: RunnerCapability,
    prompt: bytes,
    authorization: TrialAuthorization,
    binding: tuple[ScheduledCall, LearnerBridge | None],
) -> bytes:
    """Recheck configuration and attempt immediately before activation."""
    schedule, learner = binding
    validate_execution(account, context, authorization, schedule)
    fresh = prepare_capability_trial(
        context, capability, prompt, authorization, schedule.protocol
    )
    execution_terms(
        account, capability, schedule.id, authorization.candidate_digest
    )
    frame = activation(
        fresh.cli_argv,
        fresh.cli_cwd,
        fresh.cli_env,
        fresh.prompt,
        context.policy.max_input_bytes,
    )
    if learner is not None:
        review_prompt.refresh(learner.home, learner.context)
    return frame


def execute_trial(
    account: Account,
    context: ReviewContext,
    capability: RunnerCapability,
    prompt: bytes,
    authorization: TrialAuthorization,
    schedule: ScheduledCall,
) -> AccountCall:
    """Preserve the original API over the sole launch implementation."""
    return execute_result(
        account, context, capability, prompt, authorization, schedule
    ).call
