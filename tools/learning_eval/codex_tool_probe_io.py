"""Bound synthetic HTTP request evidence and owned native probe processes."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import socket
import subprocess
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast

from muninn.review_process_identity import sole_zombie
from tools.learning_eval.cases import strict_json

BODY_LIMIT = 1024 * 1024
CANARY = "MUNINN_SYNTHETIC_SOURCE_CANARY"


def _tools(value: object, prefix: str = "", depth: int = 0) -> list[str]:
    """Read both built-in tools and explicitly nested native namespaces."""
    if not isinstance(value, list) or depth > 8:
        raise ValueError("tool_schema_invalid")
    entries = cast(list[Any], value)
    if len(entries) > 256:
        raise ValueError("tool_schema_invalid")
    names: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("tool_schema_invalid")
        tool = cast(dict[str, Any], item)
        if not isinstance(tool.get("type"), str):
            raise ValueError("tool_schema_invalid")
        kind = tool["type"]
        name = tool.get("name", kind)
        if not isinstance(name, str) or not name:
            raise ValueError("tool_schema_invalid")
        if kind == "namespace":
            names.extend(
                _tools(tool.get("tools"), prefix + name + ".", depth + 1)
            )
        else:
            names.append(prefix + name)
    return names


def request_evidence(raw: bytes) -> dict[str, object]:
    """Inspect explicit tools without treating absent fields as empty."""
    if len(raw) > BODY_LIMIT:
        raise ValueError("request_limit")
    strict_json(raw)
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("request_shape")
    request = cast(dict[str, Any], parsed)
    names: list[str] = []
    surfaces = 0
    if "tools" in request:
        names.extend(_tools(request["tools"]))
        surfaces += 1
    items = request.get("input", [])
    if not isinstance(items, list):
        raise ValueError("request_shape")
    for item in cast(list[Any], items):
        if not isinstance(item, dict):
            raise ValueError("request_shape")
        entry = cast(dict[str, Any], item)
        if entry.get("type") == "additional_tools":
            names.extend(_tools(entry.get("tools")))
            surfaces += 1
    if not surfaces:
        raise ValueError("tool_surface_unreported")
    return {
        "tool_names": sorted(set(names)),
        "tool_surfaces": surfaces,
        "deferred_executor_mentioned": b"exec_command" in raw,
        "source_canary_seen": CANARY.encode() in raw,
        "source_markers_seen": [
            name
            for name in ("CONFIG", "AUTH_AGENTS", "CWD_AGENTS", "RULES")
            if (CANARY + "_" + name).encode() in raw
        ],
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "qualification": "unverified",
    }


def write_private(path: Path, raw: bytes) -> None:
    """Create new artifacts privately from their first byte."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _body_evidence(raw: bytes, declared: int) -> dict[str, object]:
    """Keep incomplete transport separate from a complete request payload."""
    error = "request_body_incomplete"
    if len(raw) == declared:
        try:
            return request_evidence(raw)
        except (ValueError, UnicodeError, RecursionError):
            error = "request_evidence_invalid"
    return {
        "error": error,
        "qualification": "unverified",
        "request_sha256": hashlib.sha256(raw).hexdigest(),
    }


def capture_handler(
    records: list[dict[str, object]], root: Path
) -> type[BaseHTTPRequestHandler]:
    """Build a finite fixture that never returns model output or tools."""

    class Handler(BaseHTTPRequestHandler):
        """Record bounded requests and terminate each with HTTP400."""

        timeout = 5
        request_deadline = 5.0

        def setup(self) -> None:
            """Bound each request, including trickled headers."""
            super().setup()
            self._deadline = threading.Timer(
                self.request_deadline, self._expire
            )
            self._deadline.daemon = True
            self._deadline.start()

        def _expire(self) -> None:
            """Interrupt an accepted socket even while bytes keep arriving."""
            with suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)

        def finish(self) -> None:
            """Cancel the owned deadline after closing request streams."""
            try:
                super().finish()
            finally:
                self._deadline.cancel()

        def handle(self) -> None:
            """Treat a deadline-closed peer as finite fixture termination."""
            with suppress(BrokenPipeError, ConnectionResetError):
                super().handle()

        def do_POST(self) -> None:
            """Refuse oversized or excessive traffic before body reads."""
            if self.path != "/responses":
                self.send_error(404)
                return
            length = self.headers.get("Content-Length", "")
            if (
                not length.isdecimal()
                or not 0 < int(length) <= BODY_LIMIT
                or len(records) >= 3
            ):
                self.send_error(413)
                return
            raw = self.rfile.read(int(length))
            path = root / f"request-{len(records) + 1}.json"
            write_private(path, raw)
            evidence = _body_evidence(raw, int(length))
            records.append(
                {"path": self.path, "raw_path": str(path), **evidence}
            )
            body = b'{"error":{"message":"synthetic probe stop"}}'
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Keep HTTP fixture output out of stdout and transcript logs."""

    return Handler


def _collect(
    process: subprocess.Popen[bytes],
    deadline: float,
    output: dict[str, bytearray],
) -> None:
    """Read finite process output without reaping its process-group leader."""
    assert process.stdout is not None and process.stderr is not None
    with selectors.DefaultSelector() as selected:
        selected.register(process.stdout, selectors.EVENT_READ, "stdout")
        selected.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selected.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("process_timeout")
            for key, _ in selected.select(remaining):
                data = os.read(key.fd, 65536)
                if not data:
                    selected.unregister(key.fileobj)
                else:
                    available = BODY_LIMIT - sum(map(len, output.values()))
                    if len(data) > available:
                        output[key.data].extend(data[:available])
                        raise ValueError("process_output_limit")
                    output[key.data].extend(data)


def _await_exit(process: subprocess.Popen[bytes], deadline: float) -> None:
    """Observe child exit without releasing its process-group identity."""
    while True:
        exited = os.waitid(
            os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
        )
        if exited is not None and exited.si_pid == process.pid:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("process_timeout")
        time.sleep(min(0.01, remaining))


def run_command(
    argv: list[str], env: dict[str, str], cwd: Path, timeout: float = 45
) -> dict[str, object]:
    """Signal the unreaped child group before publishing leader exit."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    error: str | None = None
    try:
        assert process.stdin is not None
        try:
            process.stdin.write(b'Return {"payload_json":"{}"} only.')
            process.stdin.close()
        except BrokenPipeError:
            error = "process_stdin_closed"
        deadline = time.monotonic() + timeout
        _collect(process, deadline, output)
        _await_exit(process, deadline)
    except ValueError as exc:
        error = str(exc)
    finally:
        try:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        except PermissionError:
            if not sole_zombie(process.pid):
                raise
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with suppress(BrokenPipeError):
                    stream.close()
    return {
        "exit_code": process.returncode,
        "error": error,
        "stdout_sha256": hashlib.sha256(output["stdout"]).hexdigest(),
        "stderr_sha256": hashlib.sha256(output["stderr"]).hexdigest(),
        "output_complete": error is None,
        "cleanup": "group_signal_attempted_before_leader_reap",
        "descendant_quiescence": "unverified",
        "stdout": bytes(output["stdout"]),
        "stderr": bytes(output["stderr"]),
    }
