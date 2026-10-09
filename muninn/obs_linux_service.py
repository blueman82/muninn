"""Inspect exact owned Linux units and native writer identities."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from muninn import platform_io
from muninn.obs_service_commands import arguments, render_unit
from muninn.obs_status import read_status

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PROPERTIES = ("MainPID", "ActiveState", "FragmentPath", "DropInPaths")


def read_unit(path: Path) -> str:
    """Read an ordinary private ownership file with a bounded descriptor."""
    with platform_io.open_regular(path, root=path.parent) as handle:
        platform_io.assert_private_fd(handle.fileno())
        if sys.platform != "win32":
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("unit_definition_unsafe")
        raw = handle.read(16385)
    if len(raw) > 16384:
        raise ValueError("unit_definition_too_large")
    return raw.decode()


def unit_matches(
    text: str,
    python: Path,
    release: Path,
    data: Path,
    env: Mapping[str, str],
) -> bool:
    """Match all finite owned directives, refusing duplicates and additions."""
    recorded = dict(env)
    for line in text.splitlines():
        if not line.startswith("Environment="):
            continue
        try:
            value: object = json.loads(line.removeprefix("Environment="))
        except ValueError:
            return False
        if not isinstance(value, str):
            return False
        key, separator, content = value.replace("%%", "%").partition("=")
        if not separator:
            return False
        if key == "LD_LIBRARY_PATH":
            recorded[key] = content
    wanted = render_unit(python, release, data, recorded).splitlines()
    actual = text.splitlines()
    return wanted == actual


def selection(home: Path) -> tuple[Path, Path, str]:
    """Require confined private code and the retained recorded interpreter."""
    if sys.platform == "win32":
        raise OSError("native_posix_required")
    lib = home / ".local/lib/muninn"
    current = lib / "current"
    root = current.resolve(strict=True)
    if (
        not current.is_symlink()
        or root.parent != lib.resolve(strict=True)
        or _SHA.fullmatch(root.name) is None
    ):
        raise ValueError("release_selection_invalid")
    for path in (lib, root):
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or not platform_io.is_private(path, directory=True)
        ):
            raise ValueError("release_directory_unsafe")
    python_link = lib / "python"
    if not python_link.is_symlink():
        raise ValueError("recorded_interpreter_missing")
    python = python_link.readlink()
    if not python.is_absolute():
        raise ValueError("recorded_interpreter_invalid")
    require_interpreter(python)
    return current, python, root.name


def require_interpreter(path: Path) -> None:
    """Require the same ordinary nonwritable executable used by inspection.

    Args:
        path: Selected or recorded interpreter pathname.

    Raises:
        ValueError: If the file is not ordinary or permits foreign writes.
        OSError: If the candidate cannot be inspected.
    """
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
        raise ValueError("recorded_interpreter_unsafe")


def proc_read(path: Path, limit: int) -> bytes:
    """Read a bounded native process field without publishing its contents."""
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("process_field_too_large")
    return raw


def start_identity(process: Path) -> str:
    """Require the Linux start-time field, refusing malformed stat records."""
    raw = proc_read(process / "stat", 8192).decode()
    boundary = raw.rfind(") ")
    fields = raw[boundary + 2 :].split()
    if boundary < 0 or len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("process_identity_invalid")
    return fields[19]


def process_identity(pid: int) -> tuple[str, Path, list[str]]:
    """Read exact native argv between creation-identity reads."""
    if sys.platform != "linux":
        raise OSError("native_linux_required")
    process = Path(f"/proc/{pid}")
    if process.stat().st_uid != os.getuid():
        raise ValueError("writer_owner_mismatch")
    before = start_identity(process)
    raw = proc_read(process / "cmdline", 32768)
    if not raw or not raw.endswith(b"\0"):
        raise ValueError("process_command_invalid")
    argv = raw[:-1].split(b"\0")
    executable = (process / "exe").resolve(strict=True)
    after = start_identity(process)
    if before != after:
        raise ValueError("writer_identity_changed")
    return before, executable, [os.fsdecode(value) for value in argv]


def query_unit(
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]],
) -> dict[str, str]:
    """Read only the finite native registration and process-state fields."""
    response = run(
        [
            "systemctl",
            "--user",
            "show",
            "--all",
            "muninn.service",
            "--property=" + ",".join(_PROPERTIES),
        ]
    )
    if response.returncode:
        raise ValueError("service_query_failed")
    lines = response.stdout.decode().splitlines()
    values = dict(line.split("=", 1) for line in lines)
    if len(lines) != len(_PROPERTIES) or set(values) != set(_PROPERTIES):
        raise ValueError("service_query_unknown")
    return values


def unit_stopped(
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]],
) -> bool:
    """Confirm a terminal native state without an outstanding writer.

    Returns:
        True only when the unit is inactive or failed and has no writer.

    Raises:
        ValueError: If native service state is unavailable or malformed.
        OSError: If the native service query cannot execute.
    """
    values = query_unit(run)
    pid, state = values["MainPID"], values["ActiveState"]
    if (
        re.fullmatch(r"0|[1-9][0-9]*", pid) is None
        or re.fullmatch(r"[a-z]+", state) is None
    ):
        raise ValueError("service_state_unknown")
    return pid == "0" and state in ("inactive", "failed")


def registered_unit(
    home: Path,
    user: Path,
    python: Path,
    release: Path,
    env: Mapping[str, str],
    run: Callable[[Sequence[object]], subprocess.CompletedProcess[bytes]],
) -> tuple[bool | None, str | int]:
    """Require the exact private fragment and an active unmodified unit."""
    unit = user / ".config/systemd/user/muninn.service"
    variables = {
        "HOME": str(user),
        "MUNINN_HOME": str(home),
        "CODEX_HOME": env.get("CODEX_HOME", str(user / ".codex")),
        "CLAUDE_CONFIG_DIR": env.get(
            "CLAUDE_CONFIG_DIR", str(user / ".claude")
        ),
    }
    if not unit_matches(read_unit(unit), python, release, home, variables):
        return False, "unit_definition_mismatch"
    values = query_unit(run)
    if values["FragmentPath"] != str(unit) or values["DropInPaths"]:
        return False, "unit_registration_mismatch"
    if values["ActiveState"] != "active" or not values["MainPID"].isdigit():
        return False, "writer_not_running"
    return True, int(values["MainPID"])


def inspect(
    home: Path,
    env: Mapping[str, str],
    run: Callable[[Sequence[object]], subprocess.CompletedProcess[bytes]],
) -> tuple[bool | None, str | int]:
    """Verify registered unit, actual interpreter, argv and loaded release."""
    user = Path(env.get("HOME") or Path.home())
    release, python, sha = selection(user)
    registered, identifier = registered_unit(
        home, user, python, release, env, run
    )
    if registered is not True:
        return registered, identifier
    assert isinstance(identifier, int)
    pid = identifier
    status = read_status(home)
    if not pid or status.get("pid") != pid:
        return False, "writer_identity_mismatch"
    if status.get("writer_install_sha") != sha:
        return False, "writer_release_mismatch"
    identity, executable, argv = process_identity(pid)
    if executable != python.resolve(strict=True) or argv != [
        str(python),
        *arguments(release, home, "serve", "--interval", "60"),
    ]:
        return False, "writer_command_mismatch"
    if (
        selection(user) != (release, python, sha)
        or process_identity(pid)[0] != identity
    ):
        return None, "writer_identity_changed"
    return True, pid
