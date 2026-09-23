"""Maintain the daemon-owned evidence index and operational state."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

from context import evidence_packet_from_connection
from health import audit_bytes, source_statuses, unavailable_packet
from normalizers import discover, parse_source_incremental, source_identity
from runtime import append_audit, load_json, write_json
from wal_store import (
    advance_source,
    checkpoint,
    erase_source,
    initialize,
    mark_issue,
    mark_missing,
    open_store,
    source_rows,
)

MAX_PACKET_BYTES = 2_400


def _counter(state: Mapping[str, object], name: str) -> int:
    """Read one non-negative persisted operational counter."""
    value = state.get(name)
    return value if isinstance(value, int) and value >= 0 else 0


def _integer(value: object) -> int:
    """Return a non-negative persisted cursor component."""
    return value if isinstance(value, int) and value >= 0 else 0


class Brain:
    """Own the service snapshot, reconciliation loop, and redacted state."""

    def __init__(
        self,
        codex_root: Path,
        claude_root: Path,
        database: Path,
        state_dir: Path,
    ) -> None:
        self.codex_root = codex_root
        self.claude_root = claude_root
        self.database = database
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.generation = 0
        self.last_publish = 0.0
        self.last_scan = 0.0
        self.last_error = ""
        self.pending = 0
        self.snapshot_hash = ""
        self.reconciling = False
        self.roots_unavailable = False
        self.lock = threading.Lock()
        self.audit_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        prior = load_json(self.state_dir / "state.json")
        self.audit_rotations = _counter(prior, "audit_rotations")
        self.audit_dropped = _counter(prior, "audit_dropped")
        self.requests_rejected = _counter(prior, "requests_rejected")
        self.requests_saturated = _counter(prior, "requests_saturated")
        self.unreported_rejections = 0
        self.unreported_saturation = 0
        self.storage: dict[str, object] = {}

    def audit(self, event: str, trace_id: str, **fields: object) -> None:
        """Record a bounded operational event without retaining content."""
        with self.audit_lock:
            rotated, dropped = append_audit(
                self.state_dir, event, trace_id, **fields
            )
        with self.metrics_lock:
            self.audit_rotations += int(rotated)
            self.audit_dropped += int(dropped)

    def reject_request(self, saturated: bool) -> None:
        """Count a rejected client without synchronous audit or state I/O."""
        with self.metrics_lock:
            self.requests_rejected += 1
            self.requests_saturated += int(saturated)
            self.unreported_rejections += 1
            self.unreported_saturation += int(saturated)

    def _flush_rejections(self) -> None:
        """Write one coalesced rejection audit record outside admission."""
        with self.metrics_lock:
            rejected = self.unreported_rejections
            saturated = self.unreported_saturation
            self.unreported_rejections = 0
            self.unreported_saturation = 0
        if rejected:
            self.audit(
                "client_rejected",
                uuid.uuid4().hex,
                rejected=rejected,
                saturated=saturated,
            )

    def _metrics(self) -> dict[str, int]:
        """Return counters safe to expose in local state and status."""
        with self.metrics_lock:
            return {
                "audit_rotations": self.audit_rotations,
                "audit_dropped": self.audit_dropped,
                "requests_rejected": self.requests_rejected,
                "requests_saturated": self.requests_saturated,
            }

    def reconcile(self) -> None:
        """Advance changed cursors without replacing the whole database."""
        trace_id = uuid.uuid4().hex
        started = time.monotonic()
        with self.lock:
            self.reconciling = True
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_reconcile_store()
            changed, indexed = self._reconcile_sources(connection)
            self._finish_reconcile(
                connection, changed, indexed, trace_id, started
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
            self._record_reconcile_error(trace_id, error)
        finally:
            if connection is not None:
                connection.close()
            with self.lock:
                self.reconciling = False

    def _open_reconcile_store(self) -> sqlite3.Connection:
        rollback = self.state_dir / "v1-rollback.sqlite"
        self._migrated = initialize(self.database, rollback)
        return open_store(self.database)

    def _reconcile_sources(
        self, connection: sqlite3.Connection
    ) -> tuple[bool, int]:
        known = source_rows(connection)
        current: set[tuple[str, str]] = set()
        changed = self._migrated
        indexed = 0
        for provider, root in (
            ("codex", self.codex_root),
            ("claude", self.claude_root),
        ):
            for source_path, path, fingerprint in discover(root):
                current.add((provider, source_path))
                source_changed, count = self._reconcile_source(
                    connection,
                    provider,
                    root,
                    source_path,
                    path,
                    fingerprint,
                    known.get((provider, source_path)),
                )
                changed = changed or source_changed
                indexed += count
        with connection:
            changed = bool(mark_missing(connection, current)) or changed
        return changed, indexed

    def _reconcile_source(
        self,
        connection: sqlite3.Connection,
        provider: str,
        root: Path,
        source_path: str,
        path: Path,
        fingerprint: str,
        previous: Mapping[str, object] | None,
    ) -> tuple[bool, int]:
        if previous and previous["fingerprint"] == fingerprint:
            return False, 0
        identity = source_identity(path)
        offset, line, append = self._source_cursor(previous, identity, path)
        events, error, pending, end, end_line = parse_source_incremental(
            provider, root, path, offset, line
        )
        if error:
            with connection:
                mark_issue(
                    connection,
                    provider,
                    source_path,
                    identity,
                    fingerprint,
                    error,
                )
            return True, 0
        with connection:
            advance_source(
                connection,
                provider,
                source_path,
                identity,
                fingerprint,
                events,
                end,
                end_line,
                pending,
                not append,
            )
            if (
                os.environ.get("PROVENANCE_CONTEXT_FAILPOINT")
                == "before_source_commit"
            ):
                raise RuntimeError(
                    "deterministic_failpoint_before_source_commit"
                )
        return True, len(events)

    def _source_cursor(
        self,
        previous: Mapping[str, object] | None,
        identity: str,
        path: Path,
    ) -> tuple[int, int, bool]:
        offset = _integer(previous["cursor_bytes"] if previous else 0)
        line = _integer(previous["cursor_line"] if previous else 0)
        append = bool(
            previous
            and previous["identity"] == identity
            and previous["status"] == "active"
            and path.stat().st_size >= offset
        )
        return (offset, line, append) if append else (0, 0, False)

    def _finish_reconcile(
        self,
        connection: sqlite3.Connection,
        changed: bool,
        indexed: int,
        trace_id: str,
        started: float,
    ) -> None:
        rows = source_rows(connection).values()
        self.pending = sum(int(bool(row["pending"])) for row in rows)
        self.last_error = next(
            (
                str(row["error"] or row["status"])
                for row in rows
                if row["status"] != "active"
            ),
            "",
        )
        self.storage = checkpoint(connection, self.database)
        self.last_scan = time.time()
        self.roots_unavailable = False
        if changed:
            self._publish(trace_id, indexed, started)
        self._write_state()

    def _publish(self, trace_id: str, indexed: int, started: float) -> None:
        self.generation += 1
        self.last_publish = time.time()
        marker = f"{self.generation}:{self.database.stat().st_size}"
        self.snapshot_hash = hashlib.sha256(marker.encode()).hexdigest()
        self.audit(
            "publish",
            trace_id,
            generation=self.generation,
            indexed=indexed,
            pending=self.pending,
            duration_ms=round((time.monotonic() - started) * 1000),
            snapshot_hash=self.snapshot_hash,
        )

    def _record_reconcile_error(self, trace_id: str, error: Exception) -> None:
        self.last_error = type(error).__name__
        self.roots_unavailable = isinstance(error, OSError)
        self.last_scan = time.time()
        self._write_state()
        self.audit("reconcile_error", trace_id, error=self.last_error)

    def _write_state(self) -> None:
        self._flush_rejections()
        sources, legacy_snapshot = source_statuses(self.database)
        if legacy_snapshot:
            self.last_error = "legacy_snapshot"
        with self.state_lock:
            write_json(
                self.state_dir / "state.json",
                {
                    "generation": self.generation,
                    "last_scan": self.last_scan,
                    "last_publish": self.last_publish,
                    "snapshot_hash": self.snapshot_hash,
                    "pending_sources": self.pending,
                    "last_error": self.last_error,
                    "sources": sources,
                    "audit_bytes": audit_bytes(self.state_dir),
                    "storage": self.storage,
                    **self._metrics(),
                    "otel": "trace-compatible IDs only; exporter requires "
                    "Python >=3.10",
                },
            )

    def recall(self, request: Mapping[str, object]) -> dict[str, object]:
        """Return bounded cited evidence from the published snapshot."""
        prompt = request.get("prompt")
        if not self.lock.acquire(blocking=False):
            return unavailable_packet()
        try:
            if not self.available() or not isinstance(prompt, str):
                return unavailable_packet()
            maximum = request.get("max_bytes", MAX_PACKET_BYTES)
            repo = request.get("repo")
            with closing(sqlite3.connect(self.database)) as connection:
                packet = evidence_packet_from_connection(
                    connection,
                    prompt,
                    repo if isinstance(repo, str) else None,
                    (
                        min(max(maximum, 1), MAX_PACKET_BYTES)
                        if isinstance(maximum, int)
                        else MAX_PACKET_BYTES
                    ),
                    include_provider=True,
                )
            return packet | {"available": True}
        except sqlite3.Error:
            return unavailable_packet()
        finally:
            self.lock.release()

    def erase(self, request: Mapping[str, object]) -> dict[str, object]:
        """Erase one redacted source and all linked derived records."""
        provider = request.get("provider")
        source_id = request.get("source_id")
        if not isinstance(provider, str) or not isinstance(source_id, str):
            return {"error": "invalid_erase_request"}
        if not self.lock.acquire(blocking=False):
            return {"available": False, "erased": False}
        try:
            if self.reconciling or not self.database.exists():
                return {"available": False, "erased": False}
            connection = open_store(self.database)
            try:
                with connection:
                    erased = erase_source(connection, provider, source_id)
                self.storage = checkpoint(connection, self.database)
            finally:
                connection.close()
            if erased:
                self.generation += 1
                self.last_publish = time.time()
                self.audit("erase", uuid.uuid4().hex, provider=provider)
            self._write_state()
            return {"available": self.available(), "erased": erased}
        except sqlite3.Error:
            return {"available": False, "erased": False}
        finally:
            self.lock.release()

    def available(self) -> bool:
        """Return the fail-closed health decision shared by all clients."""
        return (
            not self.reconciling
            and not self.roots_unavailable
            and self.database.exists()
            and self.last_scan > 0
            and time.time() - self.last_scan <= 5
            and not self.pending
            and not self.last_error
        )

    def status(self) -> dict[str, object]:
        """Return persisted health with live admission and audit counters."""
        return (
            load_json(self.state_dir / "state.json")
            | self._metrics()
            | {
                "audit_bytes": audit_bytes(self.state_dir),
                "available": self.available(),
            }
        )
