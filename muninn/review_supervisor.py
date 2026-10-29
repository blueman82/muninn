"""Coordinate durable review rounds on the serve process's main thread."""

from __future__ import annotations

import os
import sqlite3
import subprocess as subprocess
import time
from contextlib import closing
from pathlib import Path

from muninn import (
    review_pause_state,
    review_prompt,
    review_recovery,
    review_snapshot,
    store,
)
from muninn.review_budget import reserve_call
from muninn.review_launch_inputs import load_capability, load_prompt
from muninn.review_models import (
    ProcessIdentity,
    RequestView,
    ReviewContext,
    ReviewPolicy,
)
from muninn.review_process import (
    ProcessEvent,
    ProcessMonitor,
    ProcessOutcome,
    WorkerLaunched,
    WorkerReady,
    cancel_registered,
)
from muninn.review_process_identity import recover_group
from muninn.review_process_protocol import activation
from muninn.review_progress import checked_policy, digest
from muninn.review_request_views import request_view
from muninn.review_requests import (
    claim_or_refuse_attempt,
    fail_attempt,
    recover_attempts,
)
from muninn.review_round_state import Round as _Round
from muninn.review_runner import RunnerCapability, prepare_launch
from muninn.review_spawn import create_wrapper
from muninn.review_supervisor_result import execute as execute_result
from muninn.review_supervisor_result import failure_code
from muninn.review_supervisor_store import (
    catch_up_due,
    database,
    halt_interrupted,
    is_contention,
    persist_event,
    retire_unlaunched,
    startup_snapshot,
)
from muninn.review_supervisor_store import (
    require_main as _main,
)


class Supervisor:
    """Own SQLite and wrapper creation while its monitor owns cleanup."""

    def __init__(self, home: Path, policy: ReviewPolicy) -> None:
        """Retain only explicit policy and bounded pending round state."""
        self.home, self.policy = home, checked_policy(policy)
        self.generation = 0
        self._owner: int | None = None
        self.stop_generation: str | None = None
        self._rounds: dict[tuple[str, int], _Round] = {}
        self._pending: list[ProcessEvent] = []
        self._next: list[_Round] = []
        self.capability: RunnerCapability | None = None
        self.artifact = b""
        self._recheck_at = 0.0
        self._catchup = (0, 0.0)
        self.monitor: ProcessMonitor

    def start(self, now: float) -> None:
        """Acquire exclusive ownership before fencing abandoned generations."""
        _main()
        if self._owner is not None:
            raise ValueError("supervisor_started")
        try:
            self._owner, self.stop_generation = review_pause_state.begin_owner(
                self.home
            )
        except BlockingIOError:
            raise ValueError("supervisor_busy") from None
        try:
            self._inputs(now)
            with database(self.home) as conn:
                snapshot = startup_snapshot(conn, self.policy)
            for _, _, _, raw in snapshot[1]:
                recover_group(ProcessIdentity.model_validate_json(raw))
            with database(self.home) as conn:
                if startup_snapshot(conn, self.policy) != snapshot:
                    raise ValueError("startup_state_changed")
                self.generation = snapshot[0] + 1
                halt_interrupted(conn, self.policy)
                conn.execute(
                    "UPDATE review_control SET generation=?",
                    (self.generation,),
                )
                recover_attempts(conn, self.generation, now)
            self.monitor = ProcessMonitor(
                self.policy, runner=self.policy.runner.runner
            )
        except BaseException:
            os.close(self._owner)
            self._owner = None
            raise

    def tick(self, now: float) -> None:
        """Reconcile retained events before reserving another finite round."""
        _main()
        if self._owner is None:
            raise ValueError("supervisor_not_started")
        self._pending.extend(self.monitor.drain())
        try:
            while self._pending:
                self._event(self._pending[0], now)
                del self._pending[0]
            if not self._ready_to_spawn(now):
                return
            self._catchup = catch_up_due(
                self.home, self.policy, now, self._catchup
            )
            for pending in tuple(self._rounds.values()):
                if not pending.spawned:
                    self._spawn(pending, now)
            self._fill(now)
        except (store.BusyError, sqlite3.OperationalError) as exc:
            if isinstance(exc, sqlite3.OperationalError) and not is_contention(
                exc
            ):
                raise

    def _fill(self, now: float) -> None:
        """Allocate worker permits without replacing existing rounds."""
        while len(self._rounds) < self.policy.concurrency:
            pending = self._next.pop(0) if self._next else self._claim(now)
            if pending is None:
                break
            key = (pending.context.attempt_id, pending.context.round_index)
            if key in self._rounds:
                raise ValueError("duplicate_round")
            self._rounds[key] = pending
            self._spawn(pending, now)

    def _ready_to_spawn(self, now: float) -> bool:
        """Preserve owner pauses while waiting for a bounded setup recheck."""
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            admitted = conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0]
        if not admitted or now < self._recheck_at:
            return False
        if not self._inputs(now):
            return False
        if self._recheck_at:
            for pending in self._rounds.values():
                pending.context = pending.context.model_copy(
                    update={
                        "deadline": time.monotonic()
                        + self.policy.deadline_seconds
                    }
                )
            self._recheck_at = 0.0
        return True

    def _inputs(self, now: float) -> bool:
        """Recheck unavailable setup without reopening owner admission."""
        if self.capability is not None and self.artifact:
            return True
        stage = "runner setup file"
        try:
            self.capability = load_capability(self.home, self.policy)
            stage = "review instructions"
            self.artifact = load_prompt(self.home, self.policy)
        except ValueError as exc:
            if not review_recovery.enabled(self.policy):
                raise
            self._recheck_at = now + 60.0
            with database(self.home) as conn:
                review_recovery.record(conn, "setup", str(exc), stage, now)
            return False
        with database(self.home) as conn:
            review_recovery.clear(conn, "setup")
        return True

    def _claim(self, now: float) -> _Round | None:
        """Close claim ownership before root-owning initial admission reads."""
        with database(self.home) as conn:
            row = conn.execute(
                "SELECT * FROM review_request "
                "WHERE state IN ('pending','waiting') "
                "AND (retry_at IS NULL OR retry_at<=?) AND policy_digest=? "
                "ORDER BY CASE WHEN trigger='history' THEN 1 ELSE 0 "
                "END,created_at,id LIMIT 1",
                (now, digest(self.policy)),
            ).fetchone()
            if row is None:
                return None
            view = request_view(row)
            attempt = claim_or_refuse_attempt(
                conn, view.id, self.generation, now
            )
            if isinstance(attempt, RequestView):
                return None
        context = ReviewContext(
            request_id=view.id,
            attempt_id=attempt.id,
            generation=self.generation,
            scope=view.scope,
            policy=self.policy,
            admitted_windows=(view.window,),
            evidence_ids=(),
            round_index=1,
            deadline=time.monotonic() + self.policy.deadline_seconds,
        )
        return _Round(context, {}, {})

    def _spawn(self, pending: _Round, now: float) -> None:
        """Reserve one unique permit and durable charge before Popen."""
        context = pending.context
        key = (context.attempt_id, context.round_index)
        if pending.spawned:
            raise ValueError("duplicate_round")
        stage = "evidence"
        try:
            if not pending.initialized:
                version = review_snapshot.read(self.home, context.scope)
                pending.context, pending.initial, pending.knowledge = (
                    review_prompt.initialize(self.home, context)
                )
                if version != review_snapshot.read(self.home, context.scope):
                    raise ValueError("rejected_revision")
                pending.context = pending.context.model_copy(
                    update={"knowledge_digest": version}
                )
                pending.initialized = True
            pending.context = review_prompt.refresh(self.home, pending.context)
            stage = "prompt"
            pending.prompt = review_prompt.assemble(
                self.artifact,
                pending.context,
                pending.initial,
                pending.knowledge,
                pending.history,
            )
            stage = "launch"
            assert self.capability is not None
            pending.launch = prepare_launch(
                pending.context, self.capability, pending.prompt
            )
            with database(self.home) as conn:
                reserve_call(conn, pending.context, now)
            pending.launch = prepare_launch(
                pending.context, self.capability, pending.prompt
            )
            self._create(pending)
            with database(self.home) as conn:
                review_recovery.clear(conn, "setup")
        except (ValueError, OSError) as exc:
            if pending.spawned:
                raise RuntimeError("monitor_ownership_invariant") from None
            if review_recovery.enabled(self.policy) and review_recovery.shared(
                str(exc)
            ):
                self._recheck_at = now + 60.0
                pending.context = pending.context.model_copy(
                    update={
                        "deadline": time.monotonic()
                        + self.policy.deadline_seconds
                    }
                )
                with database(self.home) as conn:
                    review_recovery.record(
                        conn, "setup", str(exc), "launch", now
                    )
                return
            self._fail(context, failure_code(exc, "config"), now, stage)
            del self._rounds[key]

    def _create(self, pending: _Round) -> None:
        """Create only the fixed idle wrapper outside every writer root."""
        _main()
        spec = pending.launch
        assert spec is not None
        pending.handle = create_wrapper(
            spec,
            self.monitor,
            pending.context.attempt_id,
            pending.context.round_index,
            self.policy.max_output_bytes,
        )
        pending.spawned = True

    def _event(self, event: ProcessEvent, now: float) -> None:
        """Retain the permit and event through durable reconciliation."""
        key = (event.attempt_id, event.round_index)
        pending = self._rounds[key]
        if isinstance(event, WorkerReady):
            try:
                self._persist(event, now)
                pending.context = review_prompt.refresh(
                    self.home, pending.context
                )
                assert self.capability is not None
                fresh = prepare_launch(
                    pending.context, self.capability, pending.prompt
                )
                pending.context = review_prompt.refresh(
                    self.home, pending.context
                )
                frame = activation(
                    fresh.cli_argv,
                    fresh.cli_cwd,
                    fresh.cli_env,
                    fresh.prompt,
                    self.policy.max_input_bytes,
                )
                self.monitor.activate(*key, frame)
            except ValueError:
                cancel_registered(self.monitor, *key, "activation_refused")
        elif isinstance(event, WorkerLaunched):
            self._persist(event, now)
        else:
            self._persist(event, now)
            self._result(pending, event, now)
            del self._rounds[key]

    def _persist(self, event: ProcessEvent, now: float) -> None:
        """Check live readiness before the helper's writer transaction."""
        if isinstance(event, WorkerReady):
            review_prompt.refresh(
                self.home,
                self._rounds[event.attempt_id, event.round_index].context,
            )
        persist_event(self.home, event, now)

    def _result(
        self, pending: _Round, outcome: ProcessOutcome, now: float
    ) -> None:
        """Retain only reads that passed durable outcome reconciliation."""
        if execute_result(self.home, pending, outcome, now, self._fail):
            self._next.append(pending)

    def _fail(
        self,
        context: ReviewContext,
        code: str,
        now: float,
        stage: str = "review",
    ) -> None:
        """Preserve fenced requests and uncertain calls."""
        with database(self.home) as conn:
            try:
                fail_attempt(conn, context.attempt_id, code, now)
            except ValueError as exc:
                if str(exc) != "rejected_fenced":
                    raise
            if (
                self.policy.experimental_codex is not None
                and not review_recovery.enabled(self.policy)
            ):
                conn.execute("UPDATE review_control SET admission=0")
            review_recovery.record(conn, context.request_id, code, stage, now)

    def stop(self, now: float) -> None:
        """Stop admission, contain children, and fence this generation."""
        _main()
        if self._owner is None:
            return
        self.monitor.stop_all()
        if review_recovery.enabled(self.policy):
            for event in self.monitor.drain():
                persist_event(self.home, event, now)
        with database(self.home) as conn:
            if review_recovery.enabled(self.policy):
                retire_unlaunched(
                    conn,
                    (
                        key
                        for key, pending in self._rounds.items()
                        if not pending.spawned and pending.handle is None
                    ),
                    now,
                )
            halt_interrupted(conn, self.policy)
            conn.execute(
                "UPDATE review_control SET generation=generation+1 "
                "WHERE generation=?",
                (self.generation,),
            )
            generation = conn.execute(
                "SELECT generation FROM review_control"
            ).fetchone()[0]
            recover_attempts(conn, generation, now)
        self.monitor.drain()
        self._rounds.clear()
        self._pending.clear()
        self._next.clear()
        self.artifact = b""
        self.capability = None
        assert self.stop_generation is not None
        review_pause_state.complete_owner(
            self.home, self._owner, self.stop_generation
        )
        os.close(self._owner)
        self._owner = None
