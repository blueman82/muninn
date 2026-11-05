"""Keep one execution snapshot and one refresh budget per owned round."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from muninn import (
    review_codex_launch,
    review_managed_files,
    review_prompt,
    review_runtime_records,
    review_snapshot,
)
from muninn.review_launch_spec import LaunchSpec
from muninn.review_models import ReviewPolicy
from muninn.review_process import (
    ProcessMonitor,
    WorkerLaunched,
    WorkerPrestartRefused,
)
from muninn.review_process_protocol import activation
from muninn.review_round_state import Round
from muninn.review_runner import RunnerCapability, prepare_launch
from muninn.review_runtime_models import (
    ActivationRuntime,
    ManagedCapability,
    ManagedCodexSelection,
)
from muninn.review_spawn import create_wrapper
from muninn.review_supervisor_store import database, require_main


def initialize(home: Path, pending: Round, artifact: bytes) -> None:
    """Prepare original evidence and prompt without holding a writer root."""
    if not pending.initialized:
        version = review_snapshot.read(home, pending.context.scope)
        pending.context, pending.initial, pending.knowledge = (
            review_prompt.initialize(home, pending.context)
        )
        if version != review_snapshot.read(home, pending.context.scope):
            raise ValueError("rejected_revision")
        pending.context = pending.context.model_copy(
            update={"knowledge_digest": version}
        )
        pending.initialized = True
    pending.context = review_prompt.refresh(home, pending.context)
    pending.prompt = review_prompt.assemble(
        artifact,
        pending.context,
        pending.initial,
        pending.knowledge,
        pending.history,
        correction=pending.correction,
    )


def prepare(
    pending: Round,
    capability: RunnerCapability | ManagedCapability,
    *,
    force_refresh: bool = False,
    home: Path | None = None,
    now: float | None = None,
) -> LaunchSpec:
    """Reuse a fresh checked snapshot or consume the round's single refresh."""
    previous = pending.launch
    if previous is None or previous.execution is None:
        pending.launch = prepare_launch(
            pending.context, capability, pending.prompt
        )
        if pending.first_execution is None:
            pending.first_execution = pending.launch.execution
        pending.prepared_prompt = pending.prompt
        return pending.launch
    if not force_refresh and review_managed_files.matches(previous.execution):
        return _reused(pending, previous)
    if pending.refresh_count:
        raise ValueError("runtime_race_exhausted")
    if not force_refresh and not review_managed_files.selection_changed(
        previous.execution
    ):
        raise ValueError("runtime_selection_unsafe")
    pending.refresh_count = 1
    if home is not None and now is not None:
        with database(home) as conn:
            review_runtime_records.consume_refresh(conn, pending.context, now)
    try:
        fresh = prepare_launch(pending.context, capability, pending.prompt)
    except ValueError as exc:
        if str(exc) == "runtime_selection_unavailable":
            raise ValueError("runtime_race_exhausted") from None
        raise
    if fresh.execution is None or fresh.execution == previous.execution:
        raise ValueError("runtime_race_exhausted")
    pending.launch = fresh
    pending.prepared_prompt = pending.prompt
    return fresh


def _reused(pending: Round, previous: LaunchSpec) -> LaunchSpec:
    """Keep fresh evidence when reusing the same checked execution facts."""
    if pending.prepared_prompt != pending.prompt:
        pending.launch = replace(
            previous,
            prompt=review_codex_launch.native_prompt(
                pending.context, pending.prompt
            ),
        )
        pending.prepared_prompt = pending.prompt
    assert pending.launch is not None
    return pending.launch


def activate(
    home: Path,
    pending: Round,
    policy: ReviewPolicy,
    monitor: ProcessMonitor,
    now: float,
    *,
    replacement: bool = False,
) -> None:
    """Persist an offer before transferring its frame to the monitor."""
    fresh = pending.launch
    assert fresh is not None
    pending.context = review_prompt.refresh(home, pending.context)
    if fresh.execution is not None:
        with database(home) as conn:
            review_runtime_records.offer_execution(
                conn, pending.context, fresh.execution, now
            )
    frame = activation(
        fresh.cli_argv,
        fresh.cli_cwd,
        fresh.cli_env,
        fresh.prompt,
        policy.max_input_bytes,
        runtime=(
            ActivationRuntime(
                execution=fresh.execution,
                refresh_count=pending.refresh_count,
                replacement=replacement,
            )
            if fresh.execution
            else None
        ),
    )
    monitor.activate(
        pending.context.attempt_id, pending.context.round_index, frame
    )


def refused(
    home: Path,
    pending: Round,
    event: WorkerPrestartRefused,
    capability: RunnerCapability | ManagedCapability,
    monitor: ProcessMonitor,
    now: float,
) -> None:
    """Refresh only after the owned wrapper proved no native creation."""
    if pending.launch is None or pending.launch.execution is None:
        raise ValueError("runtime_race_exhausted")
    if event.identity_digest != review_runtime_records.identity_digest(
        pending.launch.execution
    ):
        raise ValueError("runtime_race_exhausted")
    with database(home) as conn:
        review_runtime_records.append_prestart_refusal(
            conn,
            pending.context,
            event.identity_digest,
            event.code,
            now,
        )
    before = pending.launch.execution
    if (
        event.code == "runtime_changed"
        and not review_managed_files.selection_changed(before)
    ):
        raise ValueError("runtime_selection_unsafe")
    prepare(pending, capability, force_refresh=True, home=home, now=now)
    assert pending.launch is not None and pending.launch.execution is not None
    if event.code == "exec_enoent" and review_managed_files.same_execution(
        before, pending.launch.execution
    ):
        raise ValueError("runtime_race_exhausted")
    activate(
        home, pending, pending.context.policy, monitor, now, replacement=True
    )


def launched(
    home: Path, pending: Round, event: WorkerLaunched, now: float
) -> None:
    """Freeze verified actual execution separately from its earlier offer."""
    if pending.launch is not None and pending.launch.execution is not None:
        if event.identity_digest != review_runtime_records.identity_digest(
            pending.launch.execution
        ):
            raise ValueError("process_identity_unverified")
        assert event.identity_digest is not None
        with database(home) as conn:
            review_runtime_records.freeze_launched_execution(
                conn,
                pending.context,
                event.identity_digest,
                now,
            )


def create(pending: Round, monitor: ProcessMonitor, output_limit: int) -> None:
    """Create only the fixed idle wrapper outside SQLite and monitor locks."""
    require_main()
    spec = pending.launch
    assert spec is not None
    pending.handle = create_wrapper(
        spec,
        monitor,
        pending.context.attempt_id,
        pending.context.round_index,
        output_limit,
    )
    pending.spawned = True


def resume_setup(pending: Round) -> None:
    """Retain the original managed deadline across shared setup rechecks."""
    if not isinstance(pending.context.policy.runner, ManagedCodexSelection):
        pending.context = pending.context.model_copy(
            update={
                "deadline": time.monotonic()
                + pending.context.policy.deadline_seconds,
            }
        )
