"""Stream one caller-owned complete current-store metadata snapshot."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from install.context import StepFailedError
from install.host_renewal_files import capture_files, requires_key
from install.host_renewal_history import CompletedHistory, attempt_view
from install.host_renewal_jobs import RetryLimits, jobs_match
from install.host_renewal_models import (
    ContainmentJob,
    ContainmentPlan,
    HostState,
)
from install.legacy_state import OperationCheck
from install.policy_record_models import Control
from install.policy_source_format import validate_policy_source
from muninn import (
    review_pause_state,
    review_recovery,
    store_copy,
    tombstone_intent,
)
from muninn.knowledge_models import AttemptId, Digest, RequestId
from muninn.review_json import canonical_json
from muninn.review_models import AttemptView, RequestView, ReviewPolicy
from muninn.review_request_views import request_view
from muninn.review_state import LearningSelection
from muninn.tombstone_key import KEY_FILE, TOMBSTONE_FILE


@dataclass(frozen=True)
class StateScan:
    """Retain complete state hashes and the finite named job projection."""

    state: HostState
    request_ids: tuple[RequestId, ...]
    attempt_ids: tuple[AttemptId, ...]
    containment: ContainmentPlan


def metadata_frame(value: object) -> bytes:
    """Length-frame scalar types and values with the existing copy encoding."""
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    return len(raw).to_bytes(8, "big") + raw


def _row(row: dict[str, object]) -> bytes:
    """Frame exact columns, scalar types and values for one complete row."""
    return metadata_frame(
        [
            (
                key,
                type(value).__name__,
                value.hex() if isinstance(value, bytes) else value,
            )
            for key, value in row.items()
        ]
    )


class _Scan:
    """Own only scan-local deadline, hashes and finite receipt projections."""

    def __init__(
        self,
        path: Path,
        candidate: Digest,
        containment: ContainmentPlan | None,
        check: OperationCheck | None,
        timeout: float,
    ) -> None:
        """Start the one absolute deadline before any format validation."""
        self.deadline = time.monotonic() + timeout
        self.expired = False
        self.check_operation = check
        self.path, self.candidate, self.before = path, candidate, containment
        self.hashes = {
            key: hashlib.sha256()
            for key in ("work", "progress", "erasure", "invariant")
        }
        self.control: Control | None = None
        self.learning: LearningSelection | None = None
        self.requests: dict[str, RequestView] = {}
        self.attempts: dict[str, AttemptView] = {}
        self.completed = CompletedHistory()
        self.counts: dict[str, int] = {}
        self.request_ids: list[RequestId] = []
        self.attempt_ids: list[AttemptId] = []
        self.retries: dict[Digest, RetryLimits] = {}
        self.key_required = False
        self.setup_digest: str | None = None
        self.named = (
            {job.request.id: job.attempt.id for job in containment.jobs}
            if containment
            else {}
        )

    def tick(self) -> None:
        """Refuse expired scans or substituted operation evidence."""
        if self.expired or time.monotonic() >= self.deadline:
            self.expired = True
            raise StepFailedError("host_renewal_limit")
        if self.check_operation is not None:
            self.check_operation()

    def interrupt(self) -> int:
        """Interrupt SQLite work at its next programmed VM progress check."""
        self.expired = time.monotonic() >= self.deadline
        return int(self.expired)

    def observe(self, table: str, row: sqlite3.Row) -> None:
        """Build views during the same streams used for full fingerprints."""
        views = {
            "review_request": self.request,
            "review_attempt": self.attempt,
        }
        if table in views:
            views[table](row)
            return
        data = dict(row)
        if table == "review_control":
            if self.control is not None:
                raise ValueError("host_renewal_control_invalid")
            self.control = TypeAdapter(Control).validate_python(data)
        elif table == "learning_setup":
            if self.learning is not None or data.pop("singleton") != 1:
                raise ValueError("host_renewal_learning_invalid")
            self.learning = LearningSelection.model_validate(data)
        elif table == "review_call":
            if (
                row["state"] in {"reserved", "launched", "unknown"}
                or row["attempt_id"] in self.named.values()
            ):
                raise StepFailedError("host_renewal_containment_required")
        elif table == "review_receipt" and (
            row["request_id"] in self.named
            or row["attempt_id"] in self.named.values()
        ):
            raise ValueError("host_renewal_job_invalid")
        elif table == "tombstone":
            data.pop("id")
            record = tombstone_intent.canonical_record(data)
            self.key_required = self.key_required or requires_key(record)

    def request(self, row: sqlite3.Row) -> None:
        """Retain only finite request IDs and selected predecessor views."""
        ident = cast(str, row["id"])
        if row["policy_digest"] == self.candidate:
            raise StepFailedError("host_renewal_digest_reused")
        self.request_ids.append(TypeAdapter(RequestId).validate_python(ident))
        historical = ident not in self.named and self.completed.add(row)
        pointer = cast(str | None, row["current_attempt_id"])
        if self.before is None and pointer is not None and not historical:
            self.named[ident] = pointer
        if ident in self.named:
            self.requests[ident] = request_view(row)
            self.counts[ident] = 0

    def attempt(self, row: sqlite3.Row) -> None:
        """Count all attempts while projecting only named predecessors."""
        ident, request = cast(str, row["id"]), cast(str, row["request_id"])
        self.attempt_ids.append(TypeAdapter(AttemptId).validate_python(ident))
        self.completed.resolve(row)
        if request in self.named:
            self.counts[request] = self.counts.get(request, 0) + 1
        if row["ended_at"] is None and self.named.get(request) != ident:
            raise ValueError("host_renewal_job_invalid")
        if self.named.get(request) == ident:
            self.attempts[ident] = attempt_view(row)

    def invariant(
        self, table: str, data: dict[str, object]
    ) -> dict[str, object] | None:
        """Exclude only permitted containment fields on named rows."""
        excluded: set[str] = set()
        if table == "review_control":
            excluded = {"generation", "admission"}
        elif table == "review_request" and data["id"] in self.named:
            excluded = {"state", "retry_at", "current_attempt_id"}
        elif table == "review_attempt" and data["id"] in self.named.values():
            excluded = {"ended_at", "progress_at", "outcome"}
        elif table == "review_issue" and data["id"] == "setup":
            self.setup_digest = hashlib.sha256(
                metadata_frame(data)
            ).hexdigest()
            safe = {
                "id": "setup",
                **review_recovery.explain(
                    "process_identity_unverified", "recovery"
                ),
                "created_at": 0.0,
            }
            if (
                self.before
                and self.setup_digest != self.before.setup_before_digest
                and data != safe
            ):
                raise StepFailedError("host_renewal_state_changed")
            return None
        return {
            key: value for key, value in data.items() if key not in excluded
        }

    def table(self, conn: sqlite3.Connection, table: str) -> None:
        """Frame one complete ordered table with no row limit or pagination."""
        self.tick()
        columns = list(conn.execute(f'PRAGMA table_info("{table}")'))
        self.tick()
        group = (
            "progress"
            if table in {"source", "review_source", "learning_setup"}
            else "erasure" if table == "tombstone" else "work"
        )
        target = (
            self.hashes[group]
            if table != "review_control"
            else hashlib.sha256()
        )
        header = metadata_frame(
            (table, [row[1] for row in columns] if columns else "absent")
        )
        target.update(header)
        self.hashes["invariant"].update(header)
        if not columns:
            if table not in {"learning_setup", "review_issue"}:
                raise ValueError("host_renewal_table_missing")
            return
        ordered = (
            sorted((row for row in columns if row[5]), key=lambda row: row[5])
            or columns
        )
        order = ",".join(f'"{row[1]}"' for row in ordered)
        self.tick()
        cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
        self.tick()
        for row in cursor:
            self.tick()
            self.observe(table, row)
            data = {
                key: row[key]
                for key in dict(row)
                if not (table == "review_request" and key == "policy_json")
            }
            target.update(_row(data))
            invariant = self.invariant(table, data)
            if invariant is not None:
                self.hashes["invariant"].update(_row(invariant))
            self.tick()
        self.tick()

    def snapshots(self, conn: sqlite3.Connection) -> None:
        """Validate each distinct immutable policy pair exactly once."""
        self.tick()
        cursor = conn.execute(
            "SELECT DISTINCT policy_digest,policy_json FROM review_request "
            "ORDER BY policy_digest,policy_json"
        )
        self.tick()
        selected = {view.policy_digest for view in self.requests.values()}
        for row in cursor:
            self.tick()
            policy = ReviewPolicy.model_validate_json(row["policy_json"])
            raw = canonical_json(policy)
            if (
                raw != row["policy_json"]
                or hashlib.sha256(raw.encode()).hexdigest()
                != row["policy_digest"]
            ):
                raise ValueError("host_renewal_snapshot_invalid")
            if row["policy_digest"] in selected:
                self.retries[row["policy_digest"]] = (
                    policy.max_attempts,
                    tuple(policy.retry_backoff_seconds),
                )
            pair = (
                row["policy_digest"],
                hashlib.sha256(metadata_frame(tuple(row))).hexdigest(),
            )
            self.hashes["work"].update(metadata_frame(pair))
            self.hashes["invariant"].update(metadata_frame(pair))
            self.tick()
        self.tick()

    def external(self) -> review_pause_state.Witness:
        """Bind erasure intent and the exact supervisor witness."""
        files = capture_files(
            self.path.parent, self.tick, require_key=self.key_required
        )
        for name, value in (
            (KEY_FILE, files.key),
            (TOMBSTONE_FILE, files.journal),
        ):
            fact = dataclasses.asdict(value) if value is not None else None
            self.hashes["erasure"].update(metadata_frame((name, fact)))
            self.hashes["invariant"].update(metadata_frame((name, fact)))
            self.tick()
        return files.witness

    def result(self, witness: review_pause_state.Witness) -> StateScan:
        """Require complete linkage before returning scan results."""
        if (
            self.control is None
            or self.completed.references
            or set(self.requests) != set(self.named)
            or set(self.attempts) != set(self.named.values())
        ):
            raise ValueError("host_renewal_job_invalid")
        jobs = tuple(
            ContainmentJob(
                request=self.requests[request],
                attempt=self.attempts[attempt],
                attempt_count=self.counts[request],
            )
            for request, attempt in sorted(self.named.items())
        )
        if self.before and not jobs_match(self.before, jobs, self.retries):
            raise StepFailedError("host_renewal_state_changed")
        if self.before is None and any(
            job.attempt.ended_at is not None
            or job.request.current_attempt_id != job.attempt.id
            for job in jobs
        ):
            raise ValueError("host_renewal_job_invalid")
        if (
            self.before
            and self.setup_digest != self.before.setup_before_digest
            and self.setup_digest is None
        ):
            raise StepFailedError("host_renewal_state_changed")
        info = self.path.lstat()
        self.tick()
        return StateScan(
            state=HostState(
                control=self.control,
                learning=self.learning,
                progress_digest=self.hashes["progress"].hexdigest(),
                work_digest=self.hashes["work"].hexdigest(),
                erasure_digest=self.hashes["erasure"].hexdigest(),
                store_identity=[info.st_dev, info.st_ino],
            ),
            request_ids=tuple(self.request_ids),
            attempt_ids=tuple(self.attempt_ids),
            containment=ContainmentPlan(
                jobs=jobs,
                invariant_digest=self.hashes["invariant"].hexdigest(),
                setup_before_digest=(
                    self.before.setup_before_digest
                    if self.before
                    else self.setup_digest
                ),
                witness=self.before.witness if self.before else witness,
            ),
        )


def scan(
    conn: sqlite3.Connection,
    path: Path,
    *,
    candidate_digest: Digest,
    containment: ContainmentPlan | None,
    check: OperationCheck | None,
    timeout: float,
) -> StateScan:
    """Scan a fresh guarded snapshot without owning a lock or transaction.

    Args:
        conn: Caller-owned current snapshot connection.
        path: Caller-guarded private database pathname.
        candidate_digest: Proposed digest forbidden in all existing requests.
        containment: Existing named projection to retain after attempts end.
        check: Optional caller-owned installer operation recheck.
        timeout: The single installer-private programmed scan duration.

    Returns:
        Complete hashes and finite typed IDs/jobs, or no result on refusal.

    Raises:
        StepFailedError: If validation, identity or the scan deadline refuses.
        ValueError: If a caller operation check refuses.
        sqlite3.Error: If SQLite cannot remove its progress handler.
        OSError: If a caller operation check refuses its native identity.
    """
    current = _Scan(path, candidate_digest, containment, check, timeout)
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    conn.set_progress_handler(current.interrupt, 1000)
    try:
        current.tick()
        conn.execute("PRAGMA temp_store=MEMORY")
        if validate_policy_source(conn) != 2:
            raise StepFailedError("host_renewal_current_format_required")
        current.tick()
        for table in (
            *store_copy.GOVERNED_TABLES,
            "source",
            "tombstone",
            "learning_setup",
            "review_issue",
        ):
            current.table(conn, table)
        current.snapshots(conn)
        result = current.result(current.external())
        current.tick()
        return result
    except (sqlite3.Error, StepFailedError, ValueError, OSError) as exc:
        if current.expired or time.monotonic() >= current.deadline:
            raise StepFailedError("host_renewal_limit") from None
        if isinstance(exc, StepFailedError):
            raise
        if isinstance(exc, (sqlite3.Error, OSError)):
            raise StepFailedError("host_renewal_state_changed") from exc
        raise StepFailedError("host_renewal_state_invalid") from exc
    finally:
        conn.set_progress_handler(None, 0)
        conn.row_factory = previous
