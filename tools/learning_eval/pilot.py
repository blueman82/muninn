"""Run the complete serialized, explicitly synthetic pilot population."""

from __future__ import annotations

import json
import time
from contextlib import closing

from muninn import store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext
from muninn.review_supervisor import Supervisor
from tools.learning_eval.account import Account
from tools.learning_eval.capability import validate_pilot_manifest
from tools.learning_eval.execution import ExecutionEvidence, execute_result
from tools.learning_eval.native_predecessor import continuation_history
from tools.learning_eval.native_terms import (
    NATIVE_FAILURES,
    account_totals,
    bind_account,
    stop_reason,
)
from tools.learning_eval.new_muninn import learn
from tools.learning_eval.pilot_evidence import (
    immutable,
    reopen_trial,
    save_trial,
)
from tools.learning_eval.pilot_models import (
    PilotManifest,
    PilotMaterial,
    PilotResult,
    PilotTrial,
    PlannedTrial,
)
from tools.learning_eval.pilot_models import schedule as schedule
from tools.learning_eval.pilot_retrieval import observe
from tools.learning_eval.pilot_sources import (
    capture_source,
    check_home,
    create_home,
    load_material,
    source_environment,
)
from tools.learning_eval.report import pilot_report
from tools.learning_eval.task_protocol import (
    parse_answer,
    task_prompt,
)

_RETAINED_OWNERS: list[Supervisor] = []


def _one(
    manifest: PilotManifest,
    material: PilotMaterial,
    plan: PlannedTrial,
    account: Account,
    owner: Supervisor,
) -> PilotTrial:
    """Join real capture, learning, eligible exposure and a task-only call."""
    source = next(s for s in material.sources if s.case_id == plan.case_id)
    case = next(c for c in material.cases if c.id == plan.case_id)
    requests = capture_source(plan.store_home, source, manifest)
    if len(requests) != 1:
        raise ValueError("pilot_boundary_mismatch")
    learned = learn(
        manifest, plan, account, requests[0], owner.generation, owner.artifact
    )
    partial = plan.result("learning_completed").model_copy(
        update={
            "receipts": learned.receipts,
            "output_paths": learned.output_paths,
            "call_ids": learned.call_ids,
            "unnecessary_call_ids": tuple(
                s.id
                for s in plan.calls
                if s.purpose == "learning" and s.id not in learned.call_ids
            ),
        }
    )
    save_trial(manifest, partial, account, "-learning")
    try:
        if case.later_task is None:
            raise ValueError("task_unavailable")
        exposure = observe(
            plan.store_home,
            learned.context.scope,
            case.later_task,
            plan.recipe,
            learned.context.policy,
            withheld=plan.comparator == "new_withheld",
        )
        partial = partial.model_copy(update={"exposure": exposure})
        return _task(
            manifest, plan, account, learned.context, case.later_task, partial
        )
    except (ValueError, OSError) as exc:
        return partial.model_copy(
            update={"outcome": str(exc) or "task_unavailable"}
        )


def _task(
    manifest: PilotManifest,
    plan: PlannedTrial,
    account: Account,
    context: ReviewContext,
    query: str,
    partial: PilotTrial,
) -> PilotTrial:
    """Execute the task stage without inventing request or commit authority."""
    assert partial.exposure is not None
    context = context.model_copy(
        update={
            "round_index": 1,
            "deadline": time.monotonic() + context.policy.deadline_seconds,
        }
    )
    prompt = task_prompt(
        manifest.task_instructions,
        query,
        partial.exposure.prompt_context,
        context.policy.max_input_bytes,
    )
    binding = manifest.task[plan.comparator]
    call = next(s for s in plan.calls if s.purpose == "task")
    result = execute_result(
        account,
        context,
        binding.capability,
        prompt,
        binding.authorization,
        call,
        evidence=ExecutionEvidence(manifest.artifact_path),
    )
    if result.outcome is None or result.outcome.error_code is not None:
        raise ValueError("task_unavailable_evidence")
    answer = parse_answer(
        result.outcome.stdout, context.policy.max_output_bytes
    )
    return partial.model_copy(
        update={"outcome": "completed", "answer": answer.answer}
    )


def _settled(account: Account, owner: Supervisor) -> bool:
    """Require both durable ledgers reconciled before owner completion."""
    if any(not c.cleaned for c in account.calls()):
        return False
    with closing(store.connect_ro(store.db_path(owner.home))) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM review_call "
                "WHERE state IN ('reserved','launched') LIMIT 1"
            ).fetchone()
            is None
        )


def run_pilot(
    manifest: PilotManifest, *, initialize: bool = False
) -> PilotResult:
    """Run only explicit synthetic dependencies with one persistent account."""
    manifest = PilotManifest.model_validate_json(canonical_json(manifest))
    prompt = validate_pilot_manifest(manifest)
    material = load_material(manifest)
    plans = schedule(manifest, material)
    predecessor, trial_map = continuation_history(manifest, plans)
    frozen = json.dumps(
        {
            "manifest": manifest.model_dump(
                mode="json", exclude={"judgments"}
            ),
            "trials": [p.model_dump(mode="json") for p in plans],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    immutable(
        manifest.artifact_path, "schedule.json", frozen, 16 * 1024 * 1024
    )
    with Account(
        manifest.account_path, manifest.assessment, initialize=initialize
    ) as account:
        if manifest.native_terms is not None:
            bind_account(account, manifest.native_terms, time.time())
        for plan in plans:
            for call in plan.calls:
                binding = (
                    manifest.learning[plan.comparator]
                    if call.purpose == "learning"
                    else manifest.task[plan.comparator]
                )
                account.plan(call, binding.authorization)
        trials = _population(manifest, material, plans, account, prompt)
        report = pilot_report(manifest, material, trials, account)
        native = manifest.native_terms
        launches, charge = (
            account_totals(account, native)
            if native is not None
            else (None, None)
        )
        return PilotResult(
            origin=manifest.origin,
            trials=trials,
            predecessor_trials=predecessor.trials if predecessor else (),
            predecessor_trial_map=trial_map,
            predecessor_receipt_digest=(
                native.predecessor_digest if native is not None else None
            ),
            model_calls=None if native is not None else len(account.calls()),
            cli_launches=launches,
            reserved_cost_decimal=charge,
            report=report,
        )


def _population(
    manifest: PilotManifest,
    material: PilotMaterial,
    plans: tuple[PlannedTrial, ...],
    account: Account,
    prompt: bytes,
) -> tuple[PilotTrial, ...]:
    """Retain all rows and one owner across each chronological family store."""
    output: dict[str, PilotTrial] = {}
    blocked = False
    for home in dict.fromkeys(plan.store_home for plan in plans):
        group = [p for p in plans if p.store_home == home]
        owner: Supervisor | None = None
        with source_environment(home):
            try:
                for plan in group:
                    prior = reopen_trial(manifest, plan, account)
                    if prior is not None:
                        if owner is None and home.exists() and not blocked:
                            owner = _start_owner(manifest, plan, prompt)
                        output[plan.id] = prior
                        continue
                    result, owner, blocked = _execute_plan(
                        manifest,
                        material,
                        plan,
                        account,
                        prompt,
                        owner,
                        blocked,
                    )
                    charged = [
                        c
                        for c in account.calls()
                        if c.schedule.trial_id == plan.id
                    ]
                    native_failure = next(
                        (
                            c.outcome
                            for c in charged
                            if c.outcome in NATIVE_FAILURES
                        ),
                        None,
                    )
                    if (
                        manifest.native_terms is not None
                        and native_failure is not None
                    ):
                        result = result.model_copy(
                            update={"outcome": native_failure}
                        )
                    result = result.model_copy(
                        update={
                            "call_ids": tuple(c.schedule.id for c in charged),
                            "output_paths": tuple(
                                manifest.artifact_path
                                / (c.schedule.id + ".json")
                                for c in charged
                                if c.evidence_digest is not None
                            ),
                        }
                    )
                    save_trial(manifest, result, account)
                    output[plan.id] = result
            finally:
                blocked = _release_owner(account, owner) or blocked
    return tuple(output[p.id] for p in plans)


def _execute_plan(
    manifest: PilotManifest,
    material: PilotMaterial,
    plan: PlannedTrial,
    account: Account,
    prompt: bytes,
    owner: Supervisor | None,
    blocked: bool,
) -> tuple[PilotTrial, Supervisor | None, bool]:
    """Keep refused stages explicit without replacing a prior started trial."""
    stopped = (
        stop_reason(account) if manifest.native_terms is not None else None
    )
    if stopped is not None:
        return plan.result(stopped), owner, True
    if blocked:
        return plan.result("account_recovery_required"), owner, True
    if plan.comparator not in {"new_present", "new_withheld"}:
        return plan.result("comparator_unsupported"), owner, False
    started = (manifest.artifact_path / (plan.id + "-started.json")).exists()
    immutable(
        manifest.artifact_path,
        plan.id + "-started.json",
        canonical_json(plan).encode(),
        1024 * 1024,
    )
    new_owner = owner is None
    try:
        if owner is None:
            owner = _start_owner(manifest, plan, prompt, existing=started)
        if started:
            return (
                plan.result("unavailable_evidence"),
                owner,
                not _settled(account, owner),
            )
        result = _one(manifest, material, plan, account, owner)
        return result, owner, not _settled(account, owner)
    except (ValueError, OSError) as exc:
        blocked = (
            owner is None
            or owner.generation == 0
            or not _settled(account, owner)
        )
        return plan.result(str(exc) or "unavailable"), owner, blocked
    except BaseException:
        # A newly acquired owner has not yet been returned to the caller.
        if new_owner:
            _release_owner(account, owner)
        raise


def _release_owner(account: Account, owner: Supervisor | None) -> bool:
    """Complete only reconciled ownership, retaining uncertain owners."""
    if owner is None:
        return False
    if _settled(account, owner):
        owner.stop(time.time())
        return False
    _RETAINED_OWNERS.append(owner)
    return True


def _start_owner(
    manifest: PilotManifest,
    plan: PlannedTrial,
    prompt: bytes,
    *,
    existing: bool = False,
) -> Supervisor:
    """Acquire the public generation lifecycle after exact store checks."""
    if not plan.store_home.exists():
        if existing:
            raise ValueError("pilot_store_missing")
        create_home(manifest, plan, prompt)
    check_home(manifest, plan)
    owner = Supervisor(plan.store_home, manifest.assessment.policies[0])
    owner.start(time.time())
    return owner
