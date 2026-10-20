"""Coordinate durable review rounds on the serve process's main thread."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess as subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from muninn import review_prompt, review_quiescence_state, store
from muninn.review_budget import reserve_call
from muninn.review_commit import CommitContext, commit_review
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
    RunningProcess,
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
from muninn.review_rounds import FinalRequest, execute_read, parse_round
from muninn.review_runner import LaunchSpec, RunnerCapability, prepare_launch
from muninn.review_spawn import create_wrapper
from muninn.review_supervisor_store import (
    database,
    is_contention,
    persist_event,
    startup_snapshot,
)


def _main() -> None:
    """Refuse SQLite ownership and creation outside the process main thread."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("main_thread_required")


@dataclass
class _Round:
    """Hold one outstanding permit through durable terminal reconciliation."""

    context: ReviewContext
    initial: object
    knowledge: object
    history: list[tuple[object, object]] = field(
        default_factory=list[tuple[object, object]]
    )
    prompt: bytes = b""
    launch: LaunchSpec | None = None
    initialized: bool = False
    spawned: bool = False
    handle: RunningProcess | None = None


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
        self.monitor: ProcessMonitor

    def start(self, now: float) -> None:
        """Acquire exclusive ownership before fencing abandoned generations."""
        _main()
        if self._owner is not None:
            raise ValueError("supervisor_started")
        try:
            self._owner, self.stop_generation = (
                review_quiescence_state.begin_owner(self.home)
            )
        except BlockingIOError:
            raise ValueError("supervisor_busy") from None
        try:
            self.capability = load_capability(self.home, self.policy)
            self.artifact = load_prompt(self.home, self.policy)
            with database(self.home) as conn:
                snapshot = startup_snapshot(conn, self.policy)
            for _, _, _, raw in snapshot[1]:
                recover_group(ProcessIdentity.model_validate_json(raw))
            with database(self.home) as conn:
                if startup_snapshot(conn, self.policy) != snapshot:
                    raise ValueError("startup_state_changed")
                self.generation = snapshot[0] + 1
                conn.execute(
                    "UPDATE review_control SET generation=?",
                    (self.generation,),
                )
                recover_attempts(conn, self.generation, now)
            self.monitor = ProcessMonitor(self.policy)
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
            for pending in tuple(self._rounds.values()):
                if not pending.spawned:
                    self._spawn(pending, now)
            while len(self._rounds) < self.policy.concurrency:
                pending = self._next.pop(0) if self._next else self._claim(now)
                if pending is None:
                    break
                key = (pending.context.attempt_id, pending.context.round_index)
                if key in self._rounds:
                    raise ValueError("duplicate_round")
                self._rounds[key] = pending
                self._spawn(pending, now)
        except (store.BusyError, sqlite3.OperationalError) as exc:
            if isinstance(exc, sqlite3.OperationalError) and not is_contention(
                exc
            ):
                raise

    def _claim(self, now: float) -> _Round | None:
        """Close claim ownership before root-owning initial admission reads."""
        with database(self.home) as conn:
            row = conn.execute(
                "SELECT * FROM review_request "
                "WHERE state IN ('pending','waiting') "
                "AND (retry_at IS NULL OR retry_at<=?) AND policy_digest=? "
                "ORDER BY created_at,id LIMIT 1",
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
        try:
            if not pending.initialized:
                pending.context, pending.initial, pending.knowledge = (
                    review_prompt.initialize(self.home, context)
                )
                pending.initialized = True
            pending.context = review_prompt.refresh(self.home, context)
            pending.prompt = review_prompt.assemble(
                self.artifact,
                pending.context,
                pending.initial,
                pending.knowledge,
                pending.history,
            )
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
        except (ValueError, OSError):
            if pending.spawned:
                raise RuntimeError("monitor_ownership_invariant") from None
            self._fail(context, "config", now)
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
        """Execute only checked reads or the atomic final commit on main."""
        try:
            if outcome.error_code is not None:
                self._fail(
                    pending.context,
                    (
                        "deadline"
                        if outcome.error_code == "deadline"
                        else "unavailable"
                    ),
                    now,
                )
                return
            context = review_prompt.refresh(self.home, pending.context)
            request = parse_round(outcome.stdout, context)
            if isinstance(request, FinalRequest):
                with database(self.home, transaction=False) as conn:
                    commit_review(
                        conn,
                        CommitContext(
                            home=self.home,
                            request_id=context.request_id,
                            attempt_id=context.attempt_id,
                            generation=context.generation,
                            evidence_ids=context.evidence_ids,
                            now=now,
                        ),
                        request.result,
                    )
                return
            result = execute_read(self.home, request, context)
            pending.context = review_prompt.refresh(
                self.home, context
            ).model_copy(update={"round_index": context.round_index + 1})
            pending.history.append(
                (request.model_dump(mode="json"), json.loads(result))
            )
            pending.spawned = False
            pending.handle = None
            pending.launch = None
            self._next.append(pending)
        except ValueError:
            self._fail(pending.context, "rejected_validation", now)

    def _fail(self, context: ReviewContext, code: str, now: float) -> None:
        """Preserve fenced requests and uncertain calls."""
        with database(self.home) as conn:
            try:
                fail_attempt(conn, context.attempt_id, code, now)
            except ValueError as exc:
                if str(exc) != "rejected_fenced":
                    raise

    def stop(self, now: float) -> None:
        """Stop admission, contain children, and fence this generation."""
        _main()
        if self._owner is None:
            return
        self.monitor.stop_all()
        with database(self.home) as conn:
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
        review_quiescence_state.complete_owner(
            self.home, self._owner, self.stop_generation
        )
        os.close(self._owner)
        self._owner = None
