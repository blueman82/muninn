"""Run one predeclared six-call supplement using frozen learned exposures."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from muninn.review_json import canonical_json
from muninn.review_runner import check_private_path, read_private_file
from tools.learning_eval.account import Account
from tools.learning_eval.capability import validate_pilot_manifest
from tools.learning_eval.cases import digest
from tools.learning_eval.execution import ExecutionEvidence, execute_result
from tools.learning_eval.native_codex import native_prompt
from tools.learning_eval.native_terms import (
    account_totals,
    bind_account,
    stop_reason,
)
from tools.learning_eval.pilot_evidence import immutable, restore_output
from tools.learning_eval.pilot_models import (
    PilotManifest,
    PilotResult,
    PilotTrial,
)
from tools.learning_eval.pilot_sources import load_material
from tools.learning_eval.task_only_models import (
    ArtifactRef,
    SourceBinding,
    TaskOnlyManifest,
    TaskOnlyPlan,
    TaskOnlyResult,
    TaskOnlyRow,
    plans_for,
    prompt_for,
    reference,
    source_binding,
)
from tools.learning_eval.trial_models import AccountCall


def _snapshot(
    pilot: PilotManifest,
) -> tuple[str, tuple[AccountCall, ...], dict[str, str]]:
    """Read Account bytes without initialization, recovery or schema writes."""
    raw = read_private_file(
        pilot.account_path / "account.sqlite3", 4 * 1024 * 1024
    )
    try:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.deserialize(raw)
            calls = tuple(
                AccountCall.model_validate_json(r[0])
                for r in conn.execute(
                    "SELECT record FROM calls ORDER BY rowid"
                )
            )
            bindings = dict(conn.execute("SELECT trial,digest FROM results"))
    except sqlite3.DatabaseError:
        raise ValueError("task_only_account_binding") from None
    return hashlib.sha256(raw).hexdigest(), calls, bindings


def _load(
    pilot_ref: ArtifactRef, result_ref: ArtifactRef
) -> tuple[PilotManifest, PilotResult]:
    """Admit only the selected completed native pilot and original policies."""
    pilot = PilotManifest.model_validate_json(pilot_ref.read())
    result = PilotResult.model_validate_json(result_ref.read())
    validate_pilot_manifest(pilot)
    policy = pilot.assessment.policies[0]
    if (
        pilot.native_terms is None
        or pilot.native_terms.carried_launches != 2
        or pilot.origin != "native_codex_trial"
        or result.origin != pilot.origin
        or (
            policy.runner.version,
            policy.runner.model,
            policy.runner.reasoning,
        )
        != ("0.163.0", "gpt-6.1-sol", "low")
        or policy.deadline_seconds != 120
        or policy.max_input_bytes != 65536
        or policy.max_output_bytes != 32768
        or policy.max_attempts != 1
        or policy.concurrency != 1
        or set(pilot.assessment.comparators) != {"new_present", "new_withheld"}
        or result.cli_launches != 14
        or result.reserved_cost_decimal != "70"
        or len(result.trials) != 6
        or any(t.outcome != "completed" for t in result.trials)
        or {t.case_id for t in result.trials}
        != {"literal", "paraphrase", "omitted"}
    ):
        raise ValueError("task_only_pilot_binding")
    material = load_material(pilot)
    for trial in result.trials:
        case = next(c for c in material.cases if c.id == trial.case_id)
        if trial.exposure is None or trial.exposure.query != case.later_task:
            raise ValueError("task_only_source_binding")
    return pilot, result


def _sources(
    pilot: PilotManifest, result: PilotResult, calls: tuple[AccountCall, ...]
) -> tuple[SourceBinding, ...]:
    """Select exactly three source present rows and real terminal contexts."""
    if (
        len(calls) != 12
        or len({c.schedule.id for c in calls}) != 12
        or {c.schedule.id for c in calls}
        != {i for t in result.trials for i in t.call_ids}
        or any(
            not c.cleaned
            or c.state != "completed"
            or c.outcome is not None
            or c.cost_reservation_decimal != "5"
            for c in calls
        )
    ):
        raise ValueError("task_only_account_binding")
    sources: list[SourceBinding] = []
    material = load_material(pilot)
    for name in ("literal", "paraphrase", "omitted"):
        selected = [
            t
            for t in result.trials
            if t.case_id == name and t.comparator == "new_present"
        ]
        if len(selected) != 1:
            raise ValueError("task_only_source_binding")
        source, context = source_binding(pilot, selected[0], calls)
        original = next(s for s in material.sources if s.case_id == name)
        if context.admitted_windows != (original.window,):
            raise ValueError("task_only_mapping_binding")
        sources.append(source)
    return tuple(sources)


def _location(pilot: PilotManifest, result: PilotResult, path: Path) -> None:
    """Keep new artifacts outside existing evidence, Account and stores."""
    check_private_path(path, directory=True)
    protected = (
        pilot.account_path,
        pilot.artifact_path,
        *(t.store_home for t in result.trials),
        *(b.authorization.disposable_home for b in pilot.task.values()),
    )
    if any(
        path == old or old in path.parents or path in old.parents
        for old in protected
    ):
        raise ValueError("task_only_artifact_path")


def _freeze(
    manifest: TaskOnlyManifest, plans: tuple[TaskOnlyPlan, ...]
) -> str:
    """Publish immutable manifest and all six slots before any claim."""
    raw = canonical_json(manifest).encode()
    immutable(manifest.artifact_path, "manifest.json", raw, 4 * 1024 * 1024)
    immutable(
        manifest.artifact_path,
        "schedule.json",
        json.dumps(
            [p.model_dump(mode="json") for p in plans],
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        4 * 1024 * 1024,
    )
    return hashlib.sha256(raw).hexdigest()


def prepare_task_only(
    pilot_manifest: ArtifactRef, pilot_result: ArtifactRef, artifact_path: Path
) -> TaskOnlyManifest:
    """Freeze exact source bytes and six paired task calls without launches."""
    pilot, result = _load(pilot_manifest, pilot_result)
    _location(pilot, result, artifact_path)
    hashed, calls, bindings = _snapshot(pilot)
    if "native-task-only-same-state-v1" in bindings:
        raise ValueError("task_only_already_declared")
    sources = _sources(pilot, result, calls)
    if any(
        bindings.get(t.id) != digest(t.model_dump(mode="json"))
        for t in result.trials
    ):
        raise ValueError("task_only_source_binding")
    assert pilot.native_terms is not None
    manifest = TaskOnlyManifest(
        pilot_manifest=pilot_manifest,
        pilot_result=pilot_result,
        import_receipt=reference(pilot.native_terms.receipt_path),
        account_preimage=hashed,
        baseline_calls=calls,
        sources=sources,
        artifact_path=artifact_path,
    )
    _freeze(manifest, plans_for(manifest, pilot, result))
    return manifest


def _validate(
    manifest: TaskOnlyManifest,
) -> tuple[PilotManifest, PilotResult, tuple[TaskOnlyPlan, ...], str]:
    """Recheck all frozen inputs and distinguish initial import from resume."""
    pilot, result = _load(manifest.pilot_manifest, manifest.pilot_result)
    _location(pilot, result, manifest.artifact_path)
    assert pilot.native_terms is not None
    if manifest.import_receipt.path != pilot.native_terms.receipt_path:
        raise ValueError("task_only_account_binding")
    manifest.import_receipt.read()
    sources = _sources(pilot, result, manifest.baseline_calls)
    if sources != manifest.sources:
        raise ValueError("task_only_source_changed")
    plans = plans_for(manifest, pilot, result)
    hashed = _freeze(manifest, plans)
    _account_binding(manifest, pilot, result, plans, hashed)
    return pilot, result, plans, hashed


def _account_binding(
    manifest: TaskOnlyManifest,
    pilot: PilotManifest,
    result: PilotResult,
    plans: tuple[TaskOnlyPlan, ...],
    hashed: str,
) -> None:
    """Verify the initial snapshot or known retained claims under ownership."""
    before, calls, bindings = _snapshot(pilot)
    prior = bindings.get(manifest.kind)
    if (prior is None and before != manifest.account_preimage) or (
        prior is not None and prior != hashed
    ):
        raise ValueError("task_only_account_binding")
    expected = {c.schedule.id: c for c in manifest.baseline_calls}
    declared = {p.schedule.id: p.schedule for p in plans}
    if (
        any(
            next((c for c in calls if c.schedule.id == key), None) != value
            for key, value in expected.items()
        )
        or {c.schedule.id for c in calls}
        - expected.keys()
        - {p.schedule.id for p in plans}
        or any(
            c.schedule != declared[c.schedule.id]
            for c in calls
            if c.schedule.id in declared
        )
        or any(
            bindings.get(t.id) != digest(t.model_dump(mode="json"))
            for t in result.trials
        )
    ):
        raise ValueError("task_only_account_binding")


def _one(
    manifest: TaskOnlyManifest,
    pilot: PilotManifest,
    source: PilotTrial,
    plan: TaskOnlyPlan,
    account: Account,
    blocked: str | None,
) -> TaskOnlyRow:
    """Restore a spent output or launch one task through the shared path."""
    call = next(
        (c for c in account.calls() if c.schedule.id == plan.schedule.id), None
    )
    prior = _prior_row(manifest, plan, account)
    if (
        prior is not None
        and prior.outcome != "completed"
        and prior.output_path is None
    ):
        return prior
    if prior is not None and call is None:
        raise ValueError("task_only_result_binding")
    if call is None and blocked is not None:
        return TaskOnlyRow(plan=plan, outcome=blocked)
    try:
        frozen, context = source_binding(
            pilot, source, manifest.baseline_calls
        )
        if frozen not in manifest.sources:
            raise ValueError("task_only_source_changed")
        prompt = prompt_for(pilot, source, plan.schedule.comparator)
        actual = native_prompt(
            prompt, plan.schedule.protocol, context.policy.max_input_bytes
        )
        if hashlib.sha256(actual).hexdigest() != plan.prompt_digest:
            raise ValueError("task_only_prompt_binding")
        binding = pilot.task[plan.schedule.comparator]
        if call is None:
            execution = execute_result(
                account,
                context,
                binding.capability,
                prompt,
                binding.authorization,
                plan.schedule,
                evidence=ExecutionEvidence(manifest.artifact_path),
            )
        else:
            execution = restore_output(
                manifest.artifact_path,
                call,
                actual,
                None,
                context.policy.max_output_bytes * 8 + 65536,
            )
        return TaskOnlyRow.from_execution(
            plan, execution, context.policy.max_output_bytes
        )
    except (ValueError, OSError) as exc:
        return TaskOnlyRow(plan=plan, outcome=str(exc) or "task_only_refused")


def _prior_row(
    manifest: TaskOnlyManifest,
    plan: TaskOnlyPlan,
    account: Account,
) -> TaskOnlyRow | None:
    """Keep refused and stopped rows terminal even without spent claims."""
    path = manifest.artifact_path / (plan.schedule.trial_id + ".json")
    bound = account.result_binding(plan.schedule.trial_id)
    if not path.exists():
        if bound is not None:
            raise ValueError("task_only_result_binding")
        return None
    raw = read_private_file(path, 4 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != bound:
        raise ValueError("task_only_result_binding")
    row = TaskOnlyRow.model_validate_json(raw)
    if row.plan != plan:
        raise ValueError("task_only_result_binding")
    return row


def run_task_only(manifest: TaskOnlyManifest) -> TaskOnlyResult:
    """Spend at most six task claims in the existing successor Account."""
    manifest = TaskOnlyManifest.model_validate_json(canonical_json(manifest))
    pilot, source_result, plans, hashed = _validate(manifest)
    assert pilot.native_terms is not None
    with Account(
        pilot.account_path, pilot.assessment, initialize=False
    ) as account:
        _account_binding(manifest, pilot, source_result, plans, hashed)
        blocked = stop_reason(account)
        if any(
            not c.cleaned or c.state != "completed" for c in account.calls()
        ):
            blocked = blocked or "task_only_stopped_after_uncertain_outcome"
        try:
            bind_account(account, pilot.native_terms, time.time())
        except ValueError as exc:
            if str(exc) != blocked:
                raise
        account.result_binding(manifest.kind, hashed)
        for plan in plans:
            account.plan(
                plan.schedule,
                pilot.task[plan.schedule.comparator].authorization,
            )
        rows: list[TaskOnlyRow] = []
        for plan in plans:
            total, charge = account_totals(account, pilot.native_terms)
            if blocked is None and (total >= 20 or Decimal(charge) + 5 > 100):
                blocked = "task_only_budget_limit"
            source = next(
                t for t in source_result.trials if t.id == plan.source_trial_id
            )
            row = _one(manifest, pilot, source, plan, account, blocked)
            raw = canonical_json(row).encode()
            immutable(
                manifest.artifact_path,
                plan.schedule.trial_id + ".json",
                raw,
                4 * 1024 * 1024,
            )
            account.result_binding(
                plan.schedule.trial_id, hashlib.sha256(raw).hexdigest()
            )
            rows.append(row)
            if row.outcome != "completed":
                blocked = "task_only_stopped_after_uncertain_outcome"
        total, charge = account_totals(account, pilot.native_terms)
        result = TaskOnlyResult(
            manifest_digest=hashed,
            source_result_digest=manifest.pilot_result.sha256,
            rows=tuple(rows),
            incremental_cli_launches=len(account.calls()) - 12,
            incremental_reserved_cost_decimal=str(Decimal(charge) - 70),
            cumulative_cli_launches=total,
            cumulative_reserved_cost_decimal=charge,
        )
        raw = canonical_json(result).encode()
        immutable(manifest.artifact_path, "result.json", raw, 4 * 1024 * 1024)
        account.result_binding(
            manifest.kind + "-result", hashlib.sha256(raw).hexdigest()
        )
        return result
