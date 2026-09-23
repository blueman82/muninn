"""Run the private Unix-socket transport for the provenance service."""

from __future__ import annotations

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

from brain import Brain
from runtime import acquire_lock, private_directory, release_lock
from runtime import request as socket_request

MAX_REQUEST_BYTES = 8_192
REQUEST_TIMEOUT_SECONDS = 0.2
MAX_CLIENTS = 8


def _json(value: object) -> bytes:
    encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        return b'{"error":"response_too_large"}\n'
    return encoded


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
            response = self._dispatch(operation, request)
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

    def _dispatch(
        self, operation: object, request: Mapping[str, object]
    ) -> dict[str, object]:
        if operation == "recall":
            return self.brain.recall(request)
        if operation == "erase":
            return self.brain.erase(request)
        if operation in {"status", "doctor"}:
            return self.brain.status()
        return {"error": "unsupported_operation"}


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
    """Serve bounded requests while reconciling the owned evidence store."""
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
