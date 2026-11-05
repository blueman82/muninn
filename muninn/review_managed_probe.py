"""Negotiate advertised Codex arguments with bounded isolated fixed probes."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import tempfile
import time
from pathlib import Path

from pydantic import ValidationError

from muninn import (
    file_sync,
    platform_io,
    review_codex_launch,
    review_managed_files,
    review_runner,
)
from muninn.review_json import canonical_json
from muninn.review_runtime_models import (
    FileStamp,
    ManagedCodexSelection,
    RuntimeIdentity,
)

_OUTPUT_LIMIT = 65536
_FLAGS = {
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--disable",
    "--sandbox",
    "--skip-git-repo-check",
    "--output-schema",
    "--json",
    "--color",
    "--model",
    "--config",
    "--cd",
}
_COMMANDS = (("--version",), ("--help",), ("exec", "--help"))


def _profile(identity: RuntimeIdentity) -> str:
    """Deny writes, network and reads outside package/loader inputs."""
    reads = " ".join(
        f"(subpath {json.dumps(str(path))})"
        for path in (
            identity.package_root,
            Path("/System"),
            Path("/usr/lib"),
            Path("/private/var/db/dyld"),
        )
    )
    return (
        "(version 1)(deny default)(allow process-exec)"
        f"(allow file-read* {reads})"
        '(allow file-read-metadata (literal "/var/empty") '
        '(literal "/private/var/empty") (literal "/dev/null"))'
    )


def _read_output(proc: subprocess.Popen[bytes], deadline: float) -> bytes:
    """Drain at most sixty-four KiB without unbounded communicate buffers."""
    assert proc.stdout is not None
    output = bytearray()
    os.set_blocking(proc.stdout.fileno(), False)
    with selectors.DefaultSelector() as ready:
        ready.register(proc.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("runtime_probe_unavailable")
            if not ready.select(min(remaining, 0.1)):
                continue
            raw = os.read(
                proc.stdout.fileno(),
                min(4096, _OUTPUT_LIMIT + 1 - len(output)),
            )
            if not raw:
                break
            output.extend(raw)
            if len(output) > _OUTPUT_LIMIT:
                raise ValueError("runtime_probe_unavailable")
    if proc.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
        raise ValueError("runtime_probe_unavailable")
    return bytes(output)


def run_probe(
    identity: RuntimeIdentity, args: tuple[str, ...], deadline: float
) -> bytes:
    """Run a fixed model-free command inside mandatory Darwin isolation."""
    if args not in _COMMANDS or time.monotonic() >= deadline:
        raise ValueError("runtime_probe_unavailable")
    try:
        with subprocess.Popen(
            (
                "/usr/bin/sandbox-exec",
                "-p",
                _profile(identity),
                identity.requested.executable,
                *args,
            ),
            cwd=Path("/var/empty"),
            env={"HOME": "/var/empty", "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        ) as proc:
            try:
                return _read_output(proc, deadline)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=1.0)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("runtime_probe_unavailable") from None


def _cache(home: Path, identity: RuntimeIdentity) -> Path:
    """Bind bounded metadata to its complete checked identity key."""
    return (
        home
        / "review-capabilities"
        / "managed-cache"
        / (identity.capability_key + ".json")
    )


def _cached(
    home: Path, identity: RuntimeIdentity
) -> tuple[bool, FileStamp | None]:
    """Treat only safe stable invalid cache content as disposable evidence."""
    path = _cache(home, identity)
    before: FileStamp | None = None
    try:
        for directory in (home, path.parent.parent, path.parent):
            review_runner.check_private_path(directory, directory=True)
            if review_codex_launch.has_write_acl(directory):
                raise ValueError("runtime_selection_unsafe")
        before = review_managed_files.stamp(path)
        if path.lstat().st_nlink != 1 or review_codex_launch.has_write_acl(
            path
        ):
            raise ValueError("runtime_selection_unsafe")
        raw = review_runner.read_private_file(path, 256 * 1024)
        if review_managed_files.stamp(path) != before:
            raise ValueError("runtime_selection_unsafe")
    except FileNotFoundError:
        if before is not None:
            raise ValueError("runtime_selection_unsafe") from None
        return False, None
    except (OSError, ValueError):
        raise ValueError("runtime_selection_unsafe") from None
    try:
        cached = RuntimeIdentity.model_validate_json(raw)
    except ValidationError:
        return False, before
    return (
        cached == identity and raw == canonical_json(identity).encode(),
        before,
    )


def _replace_cache(
    home: Path, identity: RuntimeIdentity, before: FileStamp, raw: bytes
) -> None:
    """Replace a still-safe unchanged disposable cache after fresh probes."""
    path = _cache(home, identity)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".managed-cache-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            file_sync.sync_fd(stream.fileno())
        valid, actual = _cached(home, identity)
        if valid:
            return
        if actual != before:
            raise ValueError("runtime_selection_unsafe")
        Path(temporary).replace(path)
        file_sync.sync_path(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _publish(
    home: Path, identity: RuntimeIdentity, before: FileStamp | None
) -> None:
    """Publish a observation without changing policy or any authority."""
    path = _cache(home, identity)
    for directory in (home, path.parent.parent, path.parent):
        directory.mkdir(mode=0o700, exist_ok=True)
        review_runner.check_private_path(directory, directory=True)
        if review_codex_launch.has_write_acl(directory):
            raise ValueError("runtime_selection_unsafe")
    raw = canonical_json(identity).encode()
    if len(raw) > 256 * 1024:
        raise ValueError("runtime_structure_limit")
    valid, actual = _cached(home, identity)
    if valid:
        return
    if actual != before:
        raise ValueError("runtime_selection_unsafe")
    if before is not None:
        _replace_cache(home, identity, before, raw)
    elif not platform_io.publish_private_file(path, raw):
        valid, _ = _cached(home, identity)
        if not valid:
            raise ValueError("runtime_selection_unsafe")


def negotiate(
    home: Path,
    selection: ManagedCodexSelection,
    identity: RuntimeIdentity,
    *,
    deadline: float,
    persist: bool,
) -> RuntimeIdentity:
    """Check advertised support without model or account activity."""
    if (selection.model, selection.reasoning) != (
        identity.requested.model,
        identity.requested.reasoning,
    ) or identity.package_root.parent != selection.manager_root / "releases":
        raise ValueError("runtime_selection_unsafe")
    if not review_managed_files.matches(identity):
        raise ValueError("runtime_selection_unavailable")
    deadline = min(deadline, time.monotonic() + 15.0)
    if time.monotonic() >= deadline:
        raise ValueError("runtime_probe_unavailable")
    valid, cached_before = _cached(home, identity)
    if valid:
        if time.monotonic() >= deadline:
            raise ValueError("runtime_probe_unavailable")
        if not review_managed_files.matches(identity):
            raise ValueError("runtime_selection_unavailable")
        return identity
    _advertised(identity, deadline)
    if not review_managed_files.matches(identity):
        raise ValueError("runtime_selection_unavailable")
    if time.monotonic() >= deadline:
        raise ValueError("runtime_probe_unavailable")
    if persist:
        _publish(home, identity, cached_before)
    return identity


def _advertised(identity: RuntimeIdentity, deadline: float) -> None:
    """Require every advertised argument from the same fixed probe batch."""
    try:
        version, help_text, exec_help = (
            run_probe(identity, args, deadline).decode(
                "utf-8", errors="strict"
            )
            for args in _COMMANDS
        )
    except UnicodeError:
        raise ValueError("runtime_interface_unsupported") from None
    match = re.fullmatch(
        r"codex-cli ([0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?)\s*", version
    )
    advertised = set(
        re.findall(r"--[a-z][a-z-]*", help_text + "\n" + exec_help)
    )
    if (
        match is None
        or match[1] != identity.requested.version
        or not advertised >= _FLAGS
    ):
        raise ValueError("runtime_interface_unsupported")
