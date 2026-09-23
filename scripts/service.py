"""Run and query the local, single-writer provenance context service."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingMixIn, UnixStreamServer

from context import evidence_packet_from_connection
from normalizers import discover, parse_source
from runtime import (
    acquire_lock,
    append_audit,
    load_json,
    private_directory,
    release_lock,
    write_json,
)
from runtime import request as socket_request
from snapshot import (
    delete_missing,
    mark_source_issue,
    open_snapshot,
    replace_source,
    source_rows,
)

MAX_PACKET_BYTES = 2_400
MAX_REQUEST_BYTES = 8_192
REQUEST_TIMEOUT_SECONDS = 0.2
MAX_CLIENTS = 8


def _json(value: object) -> bytes:
    encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        return b'{"error":"response_too_large"}\n'
    return encoded


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
        self.audit_rotations = 0
        self.audit_dropped = 0
        self.requests_rejected = 0
        self.requests_saturated = 0

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
        """Expose a bounded client admission rejection in health state."""
        with self.metrics_lock:
            self.requests_rejected += 1
            self.requests_saturated += int(saturated)
        self.audit(
            "client_rejected",
            uuid.uuid4().hex,
            reason="saturated" if saturated else "invalid",
        )
        self._write_state()

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
        """Publish a new snapshot when source fingerprints require it."""
        trace_id = uuid.uuid4().hex
        started = time.monotonic()
        self.reconciling = True
        with self.lock:
            temporary, connection = open_snapshot(self.database)
            try:
                known = source_rows(connection)
                current: set[tuple[str, str]] = set()
                changed = False
                indexed = 0
                pending = 0
                errors: list[str] = []
                for provider, root in (
                    ("codex", self.codex_root),
                    ("claude", self.claude_root),
                ):
                    for source_path, path, fingerprint in discover(root):
                        key = (provider, source_path)
                        current.add(key)
                        previous = known.get(key)
                        if previous and previous[0] == fingerprint:
                            pending += previous[1]
                            if previous[2]:
                                errors.append(previous[2])
                            continue
                        events, error, source_pending = parse_source(
                            provider, root, path
                        )
                        if error:
                            errors.append(error)
                            pending += 1
                            changed = (
                                mark_source_issue(
                                    connection,
                                    provider,
                                    source_path,
                                    fingerprint,
                                    error,
                                )
                                or changed
                            )
                            continue
                        indexed += replace_source(
                            connection,
                            provider,
                            source_path,
                            fingerprint,
                            events,
                            source_pending,
                        )
                        changed = True
                        pending += int(source_pending)
                if delete_missing(connection, current):
                    changed = True
                self.last_scan = time.time()
                self.pending = pending
                self.last_error = errors[0] if errors else ""
                self.roots_unavailable = False
                if changed or not self.database.exists():
                    connection.commit()
                    connection.close()
                    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, self.database)
                    self.generation += 1
                    self.last_publish = time.time()
                    self.snapshot_hash = digest
                    self.audit(
                        "publish",
                        trace_id,
                        generation=self.generation,
                        indexed=indexed,
                        pending=pending,
                        duration_ms=round((time.monotonic() - started) * 1000),
                        snapshot_hash=digest,
                    )
                else:
                    connection.close()
                    temporary.unlink(missing_ok=True)
                self._write_state()
            except (OSError, sqlite3.Error, ValueError) as error:
                connection.close()
                temporary.unlink(missing_ok=True)
                self.last_error = type(error).__name__
                self.roots_unavailable = True
                self.last_scan = time.time()
                self._write_state()
                self.audit(
                    "reconcile_error",
                    trace_id,
                    error=self.last_error,
                )
            finally:
                self.reconciling = False

    def _write_state(self) -> None:
        sources: list[dict[str, object]] = []
        if self.database.exists():
            try:
                with sqlite3.connect(self.database) as connection:
                    for (
                        provider,
                        path,
                        pending,
                        error,
                        seen,
                    ) in connection.execute(
                        "SELECT provider, source_path, pending, error, "
                        "last_seen FROM sources"
                    ):
                        sources.append(
                            {
                                "provider": provider,
                                "source_id": hashlib.sha256(
                                    path.encode()
                                ).hexdigest()[:16],
                                "pending": bool(pending),
                                "error": error,
                                "staleness_seconds": round(
                                    max(0, time.time() - seen), 3
                                ),
                            }
                        )
            except sqlite3.Error:
                self.last_error = "legacy_snapshot"
        audit_path = self.state_dir / "audit.jsonl"
        try:
            audit_bytes = audit_path.stat().st_size
        except OSError:
            audit_bytes = 0
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
                    "audit_bytes": audit_bytes,
                    **self._metrics(),
                    "otel": "trace-compatible IDs only; exporter requires "
                    "Python >=3.10",
                },
            )

    def recall(self, request: Mapping[str, object]) -> dict[str, object]:
        """Return bounded cited evidence from the published snapshot."""
        prompt = request.get("prompt")
        if not self.lock.acquire(blocking=False):
            return {"evidence": [], "bytes": 0, "untrusted": True}
        try:
            if (
                self.reconciling
                or self.roots_unavailable
                or not isinstance(prompt, str)
                or not self.database.exists()
            ):
                return {"evidence": [], "bytes": 0, "untrusted": True}
            maximum = request.get("max_bytes", MAX_PACKET_BYTES)
            maximum = maximum if isinstance(maximum, int) else MAX_PACKET_BYTES
            repo = request.get("repo")
            with sqlite3.connect(self.database) as connection:
                return evidence_packet_from_connection(
                    connection,
                    prompt,
                    repo if isinstance(repo, str) else None,
                    min(max(maximum, 1), MAX_PACKET_BYTES),
                    include_provider=True,
                )
        except sqlite3.Error:
            return {"evidence": [], "bytes": 0, "untrusted": True}
        finally:
            self.lock.release()

    def status(self) -> dict[str, object]:
        audit_path = self.state_dir / "audit.jsonl"
        try:
            audit_bytes = audit_path.stat().st_size
        except OSError:
            audit_bytes = 0
        return (
            load_json(self.state_dir / "state.json")
            | self._metrics()
            | {
                "audit_bytes": audit_bytes,
                "available": not self.reconciling
                and not self.roots_unavailable,
            }
        )


class RequestHandler(StreamRequestHandler):
    """Serve a one-line JSON request through a Unix-domain socket."""

    brain: Brain

    def handle(self) -> None:
        """Decode, service, and return a single bounded request."""
        started = time.monotonic()
        trace_id = uuid.uuid4().hex
        operation: object = None
        try:
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
            raw = self.rfile.readline(MAX_REQUEST_BYTES)
            if not raw or (
                len(raw) == MAX_REQUEST_BYTES and not raw.endswith(b"\n")
            ):
                raise ValueError("request_too_large_or_incomplete")
            request = json.loads(raw)
            if not isinstance(request, Mapping):
                raise ValueError("request")
            operation = request.get("op")
            if operation == "recall":
                response = self.brain.recall(request)
            elif operation in {"status", "doctor"}:
                response = self.brain.status()
            else:
                response = {"error": "unsupported_operation"}
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
            self.brain.reject_request(saturated=False)
            response = {"error": "invalid_request"}
        self.brain.audit(
            "client",
            trace_id,
            status="ok" if "error" not in response else "error",
            latency_ms=round((time.monotonic() - started) * 1000),
        )
        self.brain._write_state()
        if operation in {"status", "doctor"} and "error" not in response:
            response = self.brain.status()
        response["trace_id"] = trace_id
        self.wfile.write(_json(response))


class Server(ThreadingMixIn, UnixStreamServer):
    """Threaded readers around one Brain reconciliation writer."""

    brain: Brain
    slots: threading.BoundedSemaphore
    daemon_threads = True
    request_queue_size = MAX_CLIENTS

    def process_request(
        self, request: socket.socket, client_address: str
    ) -> None:
        """Reject a peer immediately when all bounded client slots are busy."""
        if not self.slots.acquire(blocking=False):
            self.brain.reject_request(saturated=True)
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(
        self, request: socket.socket, client_address: str
    ) -> None:
        """Release the client slot after the standard request lifecycle."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def request(
    socket_path: Path, payload: Mapping[str, object]
) -> dict[str, object]:
    """Request this service through its bounded private socket client."""
    return socket_request(
        socket_path, payload, REQUEST_TIMEOUT_SECONDS, MAX_REQUEST_BYTES
    )


def serve(
    codex_root: Path,
    claude_root: Path,
    database: Path,
    state_dir: Path,
    socket_path: Path,
    interval: float,
) -> int:
    private_directory(state_dir, create=True)
    private_directory(socket_path.parent, create=True)
    lock_path, lock_descriptor = acquire_lock(state_dir)
    socket_inode: int | None = None
    if socket_path.exists():
        if not stat.S_ISSOCK(socket_path.lstat().st_mode):
            release_lock(lock_path, lock_descriptor)
            raise RuntimeError("provenance socket path is not a socket")
        socket_path.unlink()
    brain = Brain(codex_root, claude_root, database, state_dir)
    brain.reconcile()
    server = Server(str(socket_path), RequestHandler)
    RequestHandler.brain = brain
    server.brain = brain
    server.slots = threading.BoundedSemaphore(MAX_CLIENTS)
    os.chmod(socket_path, 0o600)
    socket_inode = socket_path.stat().st_ino
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    next_scan = time.monotonic() + interval
    try:
        while not stop.is_set():
            server.timeout = min(0.2, max(0.01, next_scan - time.monotonic()))
            server.handle_request()
            if time.monotonic() >= next_scan:
                brain.reconcile()
                next_scan = time.monotonic() + interval
    finally:
        server.server_close()
        try:
            if socket_path.stat().st_ino == socket_inode:
                socket_path.unlink()
        except OSError:
            pass
        release_lock(lock_path, lock_descriptor)
    return 0
