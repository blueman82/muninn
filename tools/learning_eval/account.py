"""Persist one serialized harness account outside disposable runner homes."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

from muninn import platform_lock
from muninn.review_charge import aggregate, limits
from muninn.review_json import canonical_json
from muninn.review_models import PilotBudget, ProcessIdentity, Usage
from muninn.review_process_identity import identify, recover_group
from muninn.review_runner import check_private_path
from tools.learning_eval.cases import digest
from tools.learning_eval.models import Assessment, TrialAuthorization
from tools.learning_eval.trial_models import (
    AccountCall,
    ScheduledCall,
    bound_call,
    check_time,
    checked_assessment,
    reserved_charge,
    result_digest,
    validate_schedule,
)


class Account:
    """Hold the non-reentrant controller lock through spawn and cleanup."""

    def __init__(
        self, path: Path, assessment: Assessment, *, initialize: bool = False
    ) -> None:
        """Retain an explicit account path and immutable original authority."""
        self.path = path
        self.assessment = checked_assessment(assessment)
        self.initialize = initialize
        self._fd: int | None = None
        self._blocked = False

    def __enter__(self) -> Self:
        """Explicitly initialize or reopen; never replace a missing account."""
        if self._fd is not None:
            raise ValueError("account_busy")
        check_private_path(self.path, directory=True)
        try:
            self._fd = platform_lock.acquire(self.path / "controller.lock")
        except BlockingIOError:
            raise ValueError("account_busy") from None
        try:
            self._open()
            self._recover()
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release only the controller descriptor, never retained charges."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        """Close each short transaction before process creation."""
        if self._fd is None:
            raise ValueError("account_not_open")
        path = self.path / "account.sqlite3"
        check_private_path(path, directory=False)
        conn = sqlite3.connect(
            f"{path.as_uri()}?mode=rw", uri=True, isolation_level=None
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
            raise ValueError("account_invalid") from None
        finally:
            conn.close()

    def _open(self) -> None:
        """Bind immutable account identity and append authorized snapshots."""
        path = self.path / "account.sqlite3"
        if self.initialize:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        elif not path.exists():
            raise ValueError("account_missing")
        value = self.assessment
        identity = digest(
            {
                "id": value.id,
                "budget": value.budget.model_dump(mode="json"),
                "approved_by": value.approved_by,
                "approved_at": value.approved_at,
            }
        )
        with self._transaction() as conn:
            if self.initialize:
                conn.execute(
                    "CREATE TABLE metadata(identity TEXT NOT NULL, "
                    "observed_at REAL NOT NULL)"
                )
                conn.execute("INSERT INTO metadata VALUES(?,0)", (identity,))
                conn.execute(
                    "CREATE TABLE scheduled(id TEXT PRIMARY KEY, "
                    "record TEXT NOT NULL, outcome TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE snapshots(config TEXT, policy TEXT, "
                    "budget TEXT, "
                    "PRIMARY KEY(config,policy))"
                )
                conn.execute(
                    "CREATE TABLE calls(id TEXT PRIMARY KEY, slot TEXT UNIQUE "
                    "NOT NULL, record TEXT NOT NULL)"
                )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS results(trial TEXT PRIMARY KEY, "
                "digest TEXT NOT NULL)"
            )
            rows = conn.execute("SELECT identity FROM metadata").fetchall()
            if rows != [(identity,)]:
                raise ValueError("account_binding_mismatch")
            for policy in value.policies:
                if policy.budget.currency != value.budget.currency:
                    raise ValueError("budget_currency_mismatch")
                conn.execute(
                    "INSERT OR IGNORE INTO snapshots VALUES(?,?,?)",
                    (
                        value.config_digest,
                        digest(policy.model_dump(mode="json")),
                        canonical_json(policy.budget),
                    ),
                )

    def _recover(self) -> None:
        """Keep unresolved claims spent and signal only verified identities."""
        for call in self.calls():
            if call.cleaned:
                continue
            updated = call.model_copy(
                update={
                    "state": "unknown",
                    "outcome": "account_recovery_required",
                }
            )
            self._save(updated)
            try:
                if call.process_identity is None:
                    raise ValueError("process_identity_unverified")
                recover_group(call.process_identity)
            except (ValueError, OSError):
                self._blocked = True
            else:
                self._save(updated.model_copy(update={"cleaned": True}))

    def calls(self) -> tuple[AccountCall, ...]:
        """Read all retained calls without resolving unknown usage."""
        with self._transaction() as conn:
            return self._calls(conn)

    def _calls(self, conn: sqlite3.Connection) -> tuple[AccountCall, ...]:
        """Decode immutable scheduled identities and strict lifecycle rows."""
        return tuple(
            AccountCall.model_validate_json(row[0])
            for row in conn.execute("SELECT record FROM calls ORDER BY rowid")
        )

    def validate(
        self, schedule: ScheduledCall, authorization: TrialAuthorization
    ) -> None:
        """Bind an exact scheduled call to the supplied assessment snapshot."""
        validate_schedule(self.assessment, self.path, schedule, authorization)

    def claim(
        self,
        schedule: ScheduledCall,
        authorization: TrialAuthorization,
        now: float,
    ) -> tuple[AccountCall, bool]:
        """Atomically spend a durable one-use launch claim before Popen."""
        self.validate(schedule, authorization)
        declared = [old for old, _ in self.scheduled()]
        if schedule.purpose == "task" and schedule not in declared:
            if any(old.slot() == schedule.slot() for old in declared):
                raise ValueError("call_binding_mismatch")
            raise ValueError("schedule_not_declared")
        self._schedule(schedule)
        try:
            self._observe_time(now)
            return self._claim(schedule, authorization, now)
        except ValueError as exc:
            if (
                str(exc).startswith("budget_")
                or str(exc) == "account_recovery_required"
            ):
                with self._transaction() as conn:
                    conn.execute(
                        "UPDATE scheduled SET outcome=? WHERE id=?",
                        (str(exc), schedule.id),
                    )
            raise

    def plan(
        self, schedule: ScheduledCall, authorization: TrialAuthorization
    ) -> None:
        """Persist an immutable potential call without claiming spending."""
        self.validate(schedule, authorization)
        self._schedule(schedule)

    def _schedule(self, schedule: ScheduledCall) -> None:
        """Retain scheduled rows even when a cap prevents reservation."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT record FROM scheduled WHERE id=?", (schedule.id,)
            ).fetchone()
            raw = canonical_json(schedule)
            if (
                row is not None
                and ScheduledCall.model_validate_json(row[0]) != schedule
            ):
                raise ValueError("call_binding_mismatch")
            conn.execute(
                "INSERT OR IGNORE INTO scheduled VALUES(?,?,?)",
                (schedule.id, raw, "scheduled"),
            )

    def scheduled(self) -> tuple[tuple[ScheduledCall, str], ...]:
        """Expose retained scheduled and refused identities for reporting."""
        with self._transaction() as conn:
            return tuple(
                (ScheduledCall.model_validate_json(row[0]), row[1])
                for row in conn.execute(
                    "SELECT record,outcome FROM scheduled ORDER BY rowid"
                )
            )

    def _claim(
        self,
        schedule: ScheduledCall,
        authorization: TrialAuthorization,
        now: float,
    ) -> tuple[AccountCall, bool]:
        """Commit the one-use claim separately from scheduling metadata."""
        slot = schedule.slot()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT record FROM calls WHERE id=? OR slot=?",
                (schedule.id, slot),
            ).fetchone()
            if row is not None:
                existing = AccountCall.model_validate_json(row[0])
                if existing.schedule != schedule:
                    raise ValueError("call_binding_mismatch")
                return existing, False
            if self._blocked:
                raise ValueError("account_recovery_required")
            calls = self._calls(conn)
            if any(not c.cleaned for c in calls):
                raise ValueError("account_recovery_required")
            charge = reserved_charge(self.assessment, schedule.policy_digest)
            projections = [call.charge() for call in calls]
            limits(projections, self.assessment.budget, charge, now)
            for row in conn.execute("SELECT budget FROM snapshots"):
                limits(
                    projections,
                    PilotBudget.model_validate_json(row[0]),
                    charge,
                    now,
                )
            call = AccountCall(
                schedule=schedule,
                state="reserved",
                reserved_at=now,
                updated_at=now,
                cost_reservation_decimal=charge,
                currency=authorization.budget.currency,
            )
            conn.execute(
                "INSERT INTO calls VALUES(?,?,?)",
                (schedule.id, slot, canonical_json(call)),
            )
            return call, True

    def _observe_time(self, now: float) -> tuple[AccountCall, ...]:
        """Commit observed time even when a later cap check refuses work."""
        with self._transaction() as conn:
            calls = self._calls(conn)
            observed = conn.execute(
                "SELECT observed_at FROM metadata"
            ).fetchone()
            if observed is None:
                raise ValueError("account_invalid")
            check_time(now, calls, observed[0])
            conn.execute("UPDATE metadata SET observed_at=?", (now,))
            return calls

    def _save(self, call: AccountCall) -> None:
        """Persist a lifecycle transition without storing prompt or output."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE calls SET record=? WHERE id=?",
                (canonical_json(call), call.schedule.id),
            )

    def _get(self, identity: str, now: float) -> AccountCall:
        """Require an existing call and monotonic event time."""
        calls = self._observe_time(now)
        for call in calls:
            if call.schedule.id == identity:
                return call
        raise ValueError("unknown_call")

    def ready(
        self, identity: str, process: ProcessIdentity, now: float
    ) -> None:
        """Persist positively checked ready identity before activation."""
        call = self._get(identity, now)
        if call.state != "reserved" or call.process_identity is not None:
            raise ValueError("invalid_call_phase")
        if process != identify(process.pid) or process.pgid != process.pid:
            raise ValueError("process_identity_unverified")
        self._save(
            call.model_copy(
                update={"process_identity": process, "updated_at": now}
            )
        )

    def activate(self, identity: str, now: float) -> None:
        """Spend activation once after durable identity and validation."""
        call = self._get(identity, now)
        if (
            call.state != "reserved"
            or call.activation_claimed
            or call.process_identity is None
        ):
            raise ValueError("invalid_call_phase")
        self._save(
            call.model_copy(
                update={"activation_claimed": True, "updated_at": now}
            )
        )

    def launched(self, identity: str, now: float) -> None:
        """Record actual CLI launch separately from parent activation."""
        call = self._get(identity, now)
        if call.state != "reserved" or not call.activation_claimed:
            raise ValueError("invalid_call_phase")
        self._save(
            call.model_copy(update={"state": "launched", "updated_at": now})
        )

    def finish(
        self, identity: str, usage: Usage | None, code: str | None, now: float
    ) -> None:
        """Retain conservative charge after owned-child cleanup or no spawn."""
        call = self._get(identity, now)
        if call.cleaned or (code is None and call.state != "launched"):
            raise ValueError("invalid_call_phase")
        self._save(
            call.model_copy(
                update={
                    "state": "completed" if code is None else "unknown",
                    "cleaned": True,
                    "usage": usage,
                    "outcome": code,
                    "updated_at": now,
                }
            )
        )

    def result_binding(
        self, trial: str, hashed: str | None = None
    ) -> str | None:
        """Bind or read an immutable result digest without changing charges."""
        with self._transaction() as conn:
            return result_digest(conn, trial, hashed)

    def bind_evidence(self, identity: str, hashed: str) -> None:
        """Bind private output to a completed charge."""
        call = next(
            (c for c in self.calls() if c.schedule.id == identity), None
        )
        self._save(bound_call(call, hashed))

    def usage(self) -> Usage:
        """Aggregate nullable reports with shared production arithmetic."""
        return aggregate([call.charge() for call in self.calls()])

    def refuse(
        self,
        schedule: ScheduledCall,
        authorization: TrialAuthorization,
        code: str,
    ) -> None:
        """Retain refused schedules without consuming a model-call budget."""
        if code not in {"comparator_unsupported", "configuration_refused"}:
            raise ValueError("invalid_refusal_code")
        self.validate(schedule, authorization)
        self._schedule(schedule)
        with self._transaction() as conn:
            conn.execute(
                "UPDATE scheduled SET outcome=? WHERE id=?",
                (code, schedule.id),
            )

    def remaining_wall(self, now: float) -> float:
        """Bound running work by the original and historical wall ceilings."""
        calls = self._observe_time(now)
        with self._transaction() as conn:
            ceilings = [self.assessment.budget.max_wall_seconds]
            ceilings.extend(
                PilotBudget.model_validate_json(row[0]).max_wall_seconds
                for row in conn.execute("SELECT budget FROM snapshots")
            )
            return min(ceilings) - (now - min(c.reserved_at for c in calls))
