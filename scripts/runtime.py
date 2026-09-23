"""Private local-runtime ownership helpers."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
import tempfile
import time
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1] / "claude-code" / "scripts"
sys.path.insert(0, str(CORE_ROOT))
core_request = import_module("hook_core").request


MAX_AUDIT_BYTES = 1_048_576
MAX_AUDIT_RECORD_BYTES = 4_096


def request(
    socket_path: Path,
    payload: Mapping[str, object],
    timeout_seconds: float,
    max_bytes: int,
) -> dict[str, object]:
    """Delegate bounded UDS requests to the shared hook client."""
    return core_request(socket_path, payload, timeout_seconds, max_bytes)


def load_json(path: Path) -> dict[str, object]:
    """Read one local JSON object or return an empty safe state."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def write_json(path: Path, value: Mapping[str, object]) -> None:
    """Atomically write a private local JSON object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(json.dumps(value, sort_keys=True))
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


def append_audit(
    state_dir: Path, event: str, trace_id: str, **fields: object
) -> tuple[bool, bool]:
    """Append one bounded redacted audit event and report rotation or drop."""
    record = {"at": time.time(), "event": event, "trace_id": trace_id} | fields
    encoded = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_AUDIT_RECORD_BYTES:
        return False, True
    audit_path = state_dir / "audit.jsonl"
    try:
        rotated = audit_path.exists() and (
            audit_path.stat().st_size + len(encoded) > MAX_AUDIT_BYTES
        )
        if rotated:
            audit_path.replace(state_dir / "audit.previous.jsonl")
        with audit_path.open("ab") as audit_file:
            audit_file.write(encoded)
        os.chmod(audit_path, 0o600)
    except OSError:
        return False, True
    return rotated, False


def private_directory(path: Path, create: bool) -> None:
    """Require a user-owned 0700 directory before local IPC use."""
    if create:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    details = path.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise PermissionError("private runtime directory is not user-owned")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise PermissionError("private runtime directory must be mode 0700")


def acquire_lock(state_dir: Path) -> tuple[Path, int]:
    """Create an exclusive daemon lock or fail while a live owner exists."""
    lock_path = state_dir / "daemon.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise RuntimeError("provenance daemon already running")
    os.ftruncate(descriptor, 0)
    os.write(descriptor, str(os.getpid()).encode())
    return lock_path, descriptor


def release_lock(_lock_path: Path, descriptor: int) -> None:
    """Remove only the lock created by this daemon process."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    except OSError:
        return
