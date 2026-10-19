"""Map harness lifecycle events onto one real production reservation."""

from __future__ import annotations

import json
import time
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from muninn import review_prompt, store
from muninn.review_budget import call_view, reserve_call
from muninn.review_models import CallView, ReviewContext
from muninn.review_process import ProcessEvent, WorkerLaunched, WorkerReady
from muninn.review_supervisor_store import database, persist_event
from tools.learning_eval.cases import digest
from tools.learning_eval.pilot_evidence import immutable
from tools.learning_eval.trial_models import ScheduledCall


@dataclass(frozen=True)
class LearnerBridge:
    """Carry only the checked learner mapping, never executable callbacks."""

    home: Path
    context: ReviewContext
    schedule: ScheduledCall
    artifacts: Path

    def identity(self) -> dict[str, object]:
        """Bind call, store, request, attempt and round identities."""
        return {
            "home": str(self.home),
            "schedule": self.schedule.model_dump(mode="json"),
            "request_id": self.context.request_id,
            "attempt_id": self.context.attempt_id,
            "round_index": self.context.round_index,
            "generation": self.context.generation,
        }

    def reserve(self) -> None:
        """Persist mapping and reservation before wrapper creation."""
        if (
            self.schedule.purpose != "learning"
            or self.schedule.round_index != self.context.round_index
        ):
            raise ValueError("call_binding_mismatch")
        review_prompt.refresh(self.home, self.context)
        immutable(
            self.artifacts,
            self.schedule.id + "-mapping.json",
            json.dumps(
                self.identity(), sort_keys=True, separators=(",", ":")
            ).encode(),
            65536,
        )
        with database(self.home) as conn:
            value = reserve_call(conn, self.context, time.time())
        if value.state != "reserved" or value.process_identity is not None:
            raise ValueError("learner_call_spent")
        self.current()

    def current(self) -> CallView:
        """Require the exact durable reservation."""
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            row = conn.execute(
                "SELECT * FROM review_call "
                "WHERE attempt_id=? AND round_index=?",
                (self.context.attempt_id, self.context.round_index),
            ).fetchone()
            if row is None:
                raise ValueError("learner_call_missing")
            value = call_view(row)
        if (
            value.policy_digest != self.schedule.policy_digest
            or value.requested != self.context.policy.runner
            or value.budget_account_id != self.context.policy.budget.account_id
        ):
            raise ValueError("call_binding_mismatch")
        return value

    def persist(self, event: ProcessEvent) -> None:
        """Check each exact transition before and after public persistence."""
        if (event.attempt_id, event.round_index) != (
            self.schedule.id,
            self.schedule.round_index,
        ):
            raise ValueError("call_binding_mismatch")
        before = self.current()
        if isinstance(event, WorkerReady):
            review_prompt.refresh(self.home, self.context)
            if (
                before.state != "reserved"
                or before.process_identity is not None
            ):
                raise ValueError("learner_call_phase")
        elif isinstance(event, WorkerLaunched):
            if before.state != "reserved" or before.process_identity is None:
                raise ValueError("learner_call_phase")
        elif before.state not in {"reserved", "launched"} or (
            event.error_code is None and before.state != "launched"
        ):
            raise ValueError("learner_call_phase")
        mapped = replace(event, attempt_id=self.context.attempt_id)
        persist_event(self.home, mapped, time.time())
        after = self.current()
        if isinstance(event, WorkerReady):
            valid = (
                after.state == "reserved"
                and after.process_identity == event.identity
            )
        elif isinstance(event, WorkerLaunched):
            valid = (
                after.state == "launched"
                and after.launched_at == event.launched_at
            )
        else:
            valid = (
                after.state
                == ("completed" if event.error_code is None else "unknown")
                and after.usage == event.usage
                and after.observed == event.observed
                and after.outcome == (event.error_code or "completed")
                and after.ended_at is not None
            )
        if not valid:
            raise ValueError("learner_call_persistence")

    def mapping_digest(self) -> str:
        """Hash the production identity for output binding."""
        return digest(self.identity())
