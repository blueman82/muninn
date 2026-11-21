"""Run learning reviews one at a time on the serve process's main thread."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from muninn import (
    review_provider_claude,
    review_provider_codex,
    review_recovery,
    review_round_launch,
    store,
)
from muninn.decision_requests import continue_decisions
from muninn.review_budget import reserve_call
from muninn.review_call import CallOutcome, run_call
from muninn.review_instructions import artifact
from muninn.review_json import canonical_json_value
from muninn.review_models import RequestView, ReviewContext, ReviewPolicy
from muninn.review_progress import digest
from muninn.review_request_views import request_view
from muninn.review_requests import (
    claim_or_refuse_attempt,
    fail_attempt,
    recover_attempts,
)
from muninn.review_round_state import Round
from muninn.review_rounds import round_schema
from muninn.review_settings import Settings, load
from muninn.review_supervisor_result import execute, failure_code
from muninn.review_supervisor_store import (
    catch_up_due,
    database,
    is_contention,
)

_PROVIDERS = {
    "claude": review_provider_claude,
    "codex": review_provider_codex,
}
# The CLI envelope around a result carries usage data; allow for it.
_ENVELOPE_FACTOR = 4


class Learner:
    """Take one request at a time and finish it before the tick returns."""

    def __init__(self, home: Path) -> None:
        """Remember the data directory; no attempt is recovered yet."""
        self.home = home
        self._recovered = False
        self._catchup = (0, 0.0)

    def tick(self, now: float) -> None:
        """Run at most one request when learning is on.

        Args:
            now: Current time in seconds since the epoch.

        Raises:
            sqlite3.OperationalError: For a database error that is not
                contention.
        """
        # A call blocks serve for up to the policy deadline.
        # ponytail: worker thread if the heartbeat stalls.
        try:
            settings = load(self.home)
        except ValueError:
            return
        if not settings.enabled:
            return
        try:
            self._tick(settings, now)
        except store.BusyError:
            return
        except sqlite3.OperationalError as exc:
            if not is_contention(exc):
                raise

    def _tick(self, settings: Settings, now: float) -> None:
        """Recover once, continue decisions, then review one request."""
        try:
            with closing(store.connect_ro(store.db_path(self.home))) as conn:
                row = conn.execute(
                    "SELECT admission,policy_json FROM review_control"
                ).fetchone()
        except store.StoreUnavailableError:
            return
        if not row[0] or row[1] is None:
            return
        policy = ReviewPolicy.model_validate_json(row[1])
        if not self._recovered:
            self._recover(now)
        generation = self._generation()
        self._catchup = catch_up_due(self.home, policy, now, self._catchup)
        continue_decisions(self.home, generation, now)
        pending = self._claim(policy, generation, now)
        while pending is not None and self._round(settings, pending, now):
            pass

    def _recover(self, now: float) -> None:
        """Fence attempts that a stopped process left running."""
        with database(self.home) as conn:
            generation = conn.execute(
                "UPDATE review_control SET generation=generation+1 "
                "RETURNING generation"
            ).fetchone()[0]
            recover_attempts(conn, generation, now)
        self._recovered = True

    def _generation(self) -> int:
        """Read the current generation."""
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            return conn.execute(
                "SELECT generation FROM review_control"
            ).fetchone()[0]

    def _claim(
        self, policy: ReviewPolicy, generation: int, now: float
    ) -> Round | None:
        """Claim the oldest ready request, new work before history."""
        with database(self.home) as conn:
            row = conn.execute(
                "SELECT * FROM review_request WHERE candidate_id IS NULL "
                "AND state IN ('pending','waiting') "
                "AND (retry_at IS NULL OR retry_at<=?) AND policy_digest=? "
                "ORDER BY CASE WHEN trigger='history' "
                "THEN 1 ELSE 0 END,created_at,id LIMIT 1",
                (now, digest(policy)),
            ).fetchone()
            if row is None:
                return None
            view = request_view(row)
            attempt = claim_or_refuse_attempt(conn, view.id, generation, now)
            if isinstance(attempt, RequestView):
                return None
        context = ReviewContext(
            request_id=view.id,
            attempt_id=attempt.id,
            generation=generation,
            scope=view.scope,
            policy=policy,
            admitted_windows=(view.window,),
            evidence_ids=(),
            round_index=1,
            deadline=time.monotonic() + policy.deadline_seconds,
        )
        return Round(context, {}, {})

    def _round(self, settings: Settings, pending: Round, now: float) -> bool:
        """Run one call and reconcile it; True asks for another round."""
        context = pending.context
        try:
            review_round_launch.initialize(self.home, pending, artifact())
            with database(self.home) as conn:
                reserve_call(conn, pending.context, now)
        except (ValueError, OSError) as exc:
            self._fail(context, failure_code(exc, "config"), now, "evidence")
            return False
        outcome = self._call(settings, pending)
        with database(self.home) as conn:
            conn.execute(
                "UPDATE review_call SET state='completed',ended_at=?,"
                "outcome=? WHERE attempt_id=? AND round_index=?",
                (
                    now,
                    outcome.error_code or "completed",
                    context.attempt_id,
                    context.round_index,
                ),
            )
        return execute(self.home, pending, outcome, now, self._fail)

    def _call(self, settings: Settings, pending: Round) -> CallOutcome:
        """Run the provider CLI once; failures carry a code, never text."""
        provider = _PROVIDERS[settings.provider]
        executable = provider.find_executable()
        if executable is None:
            return CallOutcome(b"", "cli_missing")
        context = pending.context
        limit = context.policy.max_output_bytes * _ENVELOPE_FACTOR
        schema = canonical_json_value(round_schema())
        with tempfile.TemporaryDirectory(prefix="muninn-learn-") as name:
            work = Path(name)
            (work / "schema.json").write_text(schema)
            out = run_call(
                provider.argv(settings, schema, executable, work),
                pending.prompt,
                work,
                os.environ,
                max(context.deadline - time.monotonic(), 1.0),
                limit,
            )
        if out.error_code is not None:
            return CallOutcome(
                b"",
                "deadline" if out.error_code == "timeout" else out.error_code,
            )
        try:
            return CallOutcome(provider.decode(out.stdout, limit), None)
        except ValueError as exc:
            return CallOutcome(b"", str(exc))

    def _fail(
        self,
        context: ReviewContext,
        code: str,
        now: float,
        stage: str = "review",
    ) -> None:
        """Record the failure and let the request retry when it may."""
        with database(self.home) as conn:
            try:
                fail_attempt(conn, context.attempt_id, code, now)
            except ValueError as exc:
                if str(exc) != "rejected_fenced":
                    raise
            review_recovery.record(conn, context.request_id, code, stage, now)
