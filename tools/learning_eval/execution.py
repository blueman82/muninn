"""Run one synthetic harness call through the shared concrete containment."""

from __future__ import annotations

import time

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
from tools.learning_eval.trial_models import AccountCall, ScheduledCall


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
        context.round_index != schedule.round_index
        or digest(context.policy.model_dump(mode="json"))
        != schedule.policy_digest
        or account.assessment.comparator_config_digests[schedule.comparator]
        != digest(observed)
    ):
        raise ValueError("call_binding_mismatch")


def execute_trial(
    account: Account,
    context: ReviewContext,
    capability: RunnerCapability,
    prompt: bytes,
    authorization: TrialAuthorization,
    schedule: ScheduledCall,
) -> AccountCall:
    """Spend one durable claim before creating the shared idle wrapper."""
    try:
        validate_execution(account, context, authorization, schedule)
        prepare_capability_trial(context, capability, prompt, authorization)
    except (ValueError, OSError) as exc:
        code = (
            "comparator_unsupported"
            if str(exc) == "comparator_unsupported"
            else "configuration_refused"
        )
        account.refuse(schedule, authorization, code)
        raise
    call, permit = account.claim(schedule, authorization, time.time())
    if not permit:
        return call
    remaining = min(
        account.remaining_wall(time.time()), context.policy.deadline_seconds
    )
    context = context.model_copy(
        update={
            "deadline": min(context.deadline, time.monotonic() + remaining),
        }
    )
    monitor = ProcessMonitor(context.policy)
    try:
        fresh = prepare_capability_trial(
            context, capability, prompt, authorization
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
            schedule,
        )
    finally:
        # The independent monitor retains owned children if persistence fails.
        monitor.stop_all()
    account.finish(schedule.id, outcome.usage, outcome.error_code, time.time())
    return next(c for c in account.calls() if c.schedule.id == schedule.id)


def _wait(
    account: Account,
    monitor: ProcessMonitor,
    context: ReviewContext,
    capability: RunnerCapability,
    prompt: bytes,
    authorization: TrialAuthorization,
    schedule: ScheduledCall,
) -> ProcessOutcome:
    """Persist readiness, freshly revalidate, then spend activation once."""
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
                    validate_execution(
                        account, context, authorization, schedule
                    )
                    fresh = prepare_capability_trial(
                        context, capability, prompt, authorization
                    )
                    frame = activation(
                        fresh.cli_argv,
                        fresh.cli_cwd,
                        fresh.cli_env,
                        fresh.prompt,
                        context.policy.max_input_bytes,
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
                account.launched(schedule.id, time.time())
            else:
                return event
        time.sleep(0.01)
