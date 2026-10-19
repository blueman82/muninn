"""Bounded process and fingerprint helpers for private provider proof."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

from install import configedit
from install.constants import PLUGIN_ID
from install.context import Ctx
from install.provider_paths import codex_argv
from install.trust import TrustedHook

PROVIDER_VERSION = "codex-cli 0.163.0"
PROVIDER_SHA256 = (
    "8813367285524ac798dd96d1b071aea30d58708c7226fd236cd275cf1cfdee31"
)
SOURCE_DIRS = (
    "muninn",
    "install",
    "bin",
    "integrations",
    "tools/learning_eval",
)


@dataclass(frozen=True)
class CommandResult:
    """Captured bounded output and elapsed wall time of one subprocess."""

    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_ms: int


def file_hash(path: Path) -> str:
    """Hash a regular file's bytes without loading it all into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value: object) -> bytes:
    """Encode deterministic JSON for output and content fingerprints."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def manifest(root: Path) -> dict[str, str]:
    """Fingerprint copied runtime, provider templates and proof source files.

    Args:
        root: Candidate repository, never a real provider or transcript root.

    Returns:
        Relative names and byte hashes, excluding generated Python caches.

    Raises:
        ValueError: If source paths contain a symlink or special file.
    """
    result: dict[str, str] = {}
    for name in (*SOURCE_DIRS, "requirements-runtime.lock", "pyproject.toml"):
        base = root / name
        paths = [base, *base.rglob("*")] if base.is_dir() else [base]
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink():
                raise ValueError("candidate_alias")
            if path.is_file():
                result[path.relative_to(root).as_posix()] = file_hash(path)
            elif path.exists() and not path.is_dir():
                raise ValueError("candidate_special_file")
    return result


def environment(ctx: Ctx, python: Path) -> dict[str, str]:
    """Build an allowlisted environment with synthetic data roots."""
    return {
        "HOME": str(ctx.home),
        "USERPROFILE": str(ctx.home),
        "CODEX_HOME": str(ctx.codex_home),
        "CLAUDE_CONFIG_DIR": str(ctx.settings.parent),
        "MUNINN_HOME": str(ctx.data),
        "MUNINN_PYTHON": str(python),
        "MUNINN_ROOTS": canonical(
            {"codex-sessions": str(ctx.home / "transcripts")}
        ).decode(),
        "PATH": str(ctx.home / "provider-bin") + ":/usr/bin:/bin",
        "TMPDIR": str(ctx.home / "tmp"),
        "XDG_CONFIG_HOME": str(ctx.home / ".config"),
        "XDG_CACHE_HOME": str(ctx.home / ".cache"),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def command(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    *,
    payload: bytes = b"",
    timeout: float = 5.0,
    limit: int = 1024 * 1024,
    exchange: Callable[[bytes, IO[bytes]], bool] | None = None,
) -> CommandResult:
    """Execute without a login shell, bounding input, output and process time.

    Args:
        argv: Exact command and arguments; no model commands are constructed.
        env: Complete allowlisted environment.
        cwd: Private working directory.
        payload: At most one pipe-atomic write of synthetic JSON.
        timeout: Total deadline in seconds.
        limit: Maximum combined stdout and stderr bytes.
        exchange: Optional dialogue returning true after a complete response.

    Returns:
        Captured output, exit code and elapsed milliseconds.

    Raises:
        ValueError: If input, output or time exceeds its bound.
    """
    if len(payload) > 4096:
        raise ValueError("input_limit")
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(payload)
        process.stdin.flush()
        if exchange is None:
            process.stdin.close()
        output, complete = _collect(
            process, started + timeout, limit, exchange
        )
        remaining = timeout - (time.monotonic() - started)
        try:
            code = (
                0 if complete else process.wait(timeout=max(remaining, 0.001))
            )
        except subprocess.TimeoutExpired:
            raise ValueError("process_timeout") from None
        return CommandResult(
            code,
            bytes(output["stdout"]),
            bytes(output["stderr"]),
            round((time.monotonic() - started) * 1000),
        )
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream is not None
            stream.close()


def _collect(
    process: subprocess.Popen[bytes],
    deadline: float,
    limit: int,
    exchange: Callable[[bytes, IO[bytes]], bool] | None,
) -> tuple[dict[str, bytearray], bool]:
    """Bound aggregate output before parsing under one absolute deadline."""
    assert process.stdin and process.stdout and process.stderr
    output = {"stdout": bytearray(), "stderr": bytearray()}
    with selectors.DefaultSelector() as selected:
        for name, stream in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
        ):
            selected.register(stream, selectors.EVENT_READ, name)
        while selected.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("process_timeout")
            for key, _ in selected.select(remaining):
                chunk = os.read(key.fd, min(65536, limit + 1))
                if not chunk:
                    selected.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                if sum(map(len, output.values())) > limit:
                    raise ValueError("output_limit")
                if (
                    key.data == "stdout"
                    and exchange
                    and exchange(chunk, process.stdin)
                ):
                    return output, True
    if exchange:
        raise ValueError("incomplete_protocol")
    return output, False


def check_hooks(
    entries: list[dict[str, Any]],
    wanted: list[TrustedHook],
    cached: Path,
) -> list[dict[str, Any]]:
    """Bind every actual provider hook to expected command, hash and cache.

    Args:
        entries: Actual hooks/list reply; duplicates are forbidden.
        wanted: Locally computed identities for the exact rendered template.
        cached: Verified private cache file containing those template bytes.

    Returns:
        Managed hook evidence including the provider's trust status.

    Raises:
        ValueError: If keys, commands, current hashes or source paths differ.
    """
    if len(wanted) != 4 or {h["event"] for h in wanted} != {
        "session_start",
        "session_end",
        "pre_compact",
        "user_prompt_submit",
    }:
        raise ValueError("expected_routes_mismatch")
    got = [entry for entry in entries if entry.get("pluginId") == PLUGIN_ID]
    expected = {
        f"{PLUGIN_ID}:hooks/hooks.json:{h['suffix']}": h for h in wanted
    }
    if len(got) != len(expected) or {e.get("key") for e in got} != set(
        expected
    ):
        raise ValueError("hook_routes_mismatch")
    for entry in got:
        hook = expected[entry["key"]]
        if (
            entry.get("command") != hook["command"]
            or entry.get("currentHash") != hook["hash"]
            or entry.get("sourcePath") != str(cached)
            or not isinstance(entry.get("trustStatus"), str)
        ):
            raise ValueError("hook_identity_mismatch")
    return got


def learning_state(ctx: Ctx) -> dict[str, Any]:
    """Read admission and durable work counts without granting authority."""
    uri = (ctx.data / "muninn.sqlite").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        admission, policy = conn.execute(
            "SELECT admission,policy_json FROM review_control "
            "WHERE singleton=1"
        ).fetchone()
        return {
            "admission": admission,
            "policy_present": policy is not None,
            "requests": conn.execute(
                "SELECT COUNT(*) FROM review_request"
            ).fetchone()[0],
            "calls": conn.execute(
                "SELECT COUNT(*) FROM review_call"
            ).fetchone()[0],
        }


def artifacts(private: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Publish private proof and its content hashes in the new run root."""
    configedit.atomic_write(private / "report.json", canonical(report), 0o600)
    hashes = {
        name: file_hash(private / name)
        for name in (
            "report.json",
            "ingest.json",
            "hooks-before-trust.json",
            "learning-refusal.json",
        )
    }
    configedit.atomic_write(private / "hashes.json", canonical(hashes), 0o600)
    return {
        "private_root": str(private),
        "artifact_sha256": hashes,
        "hashes_sha256": file_hash(private / "hashes.json"),
    }


@contextmanager
def provider_environment(env: dict[str, str]) -> Generator[None]:
    """Scope the existing probe environment in this standalone process."""
    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def write_private(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Atomically write synthetic data under private parent directories."""
    missing = [p for p in path.parents if not p.exists()]
    for parent in reversed(missing):
        parent.mkdir(mode=0o700)
    configedit.atomic_write(path, content, mode)


def copy_private(source: Path, target: Path) -> None:
    """Copy a checked source tree into private ordinary files."""
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError("candidate_alias")
        if path.is_file():
            write_private(target / path.relative_to(source), path.read_bytes())


def codex_probe(ctx: Ctx, timeout: float = 10) -> list[dict[str, Any]]:
    """Resolve hooks with finite output, deadline and kill/reap cleanup."""
    pending = bytearray()
    initialized = False
    result: dict[str, Any] = {}

    def exchange(chunk: bytes, stdin: IO[bytes]) -> bool:
        nonlocal initialized
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, tail = pending.partition(b"\n")
            pending[:] = tail
            message = configedit.load_json(bytes(line))
            if message.get("id") == 1:
                if initialized or not isinstance(message.get("result"), dict):
                    raise ValueError("initialize_failed")
                initialized = True
                for request in (
                    {"method": "initialized"},
                    {
                        "id": 2,
                        "method": "hooks/list",
                        "params": {"cwds": [str(ctx.home)]},
                    },
                ):
                    stdin.write(canonical(request) + b"\n")
                stdin.flush()
            elif message.get("id") == 2:
                if not initialized or not isinstance(
                    message.get("result"), dict
                ):
                    raise ValueError("hooks_list_failed")
                result.update(message["result"])
                return True
        return False

    request = {
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "muninn-provider-proof", "version": "1"}
        },
    }
    command(
        codex_argv(ctx, "app-server"),
        dict(os.environ),
        ctx.home,
        payload=canonical(request) + b"\n",
        timeout=timeout,
        exchange=exchange,
    )
    return _hooks_result(result)


def _hooks_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Reject incomplete or malformed app-server hook result envelopes."""
    data = result.get("data")
    if not isinstance(data, list):
        raise ValueError("hooks_list_invalid")
    hooks: list[dict[str, Any]] = []
    for row in cast(list[Any], data):
        if not isinstance(row, dict):
            raise ValueError("hooks_list_invalid")
        entries = cast(dict[str, Any], row).get("hooks")
        if not isinstance(entries, list):
            raise ValueError("hooks_list_invalid")
        for hook in cast(list[Any], entries):
            if not isinstance(hook, dict):
                raise ValueError("hooks_list_invalid")
            hooks.append(cast(dict[str, Any], hook))
    return hooks
