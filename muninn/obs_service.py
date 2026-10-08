"""Native service inspection and safe command construction for doctor."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from muninn import obs_linux_service, platform_io
from muninn.obs_status import read_status
from muninn.platform_paths import read_selection, windows_base


def powershell(script: str) -> list[str]:
    """Encode a fixed native script independently of cmd metacharacters."""
    root = Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
    executable = root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        str(executable),
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def literal(value: str) -> str:
    """Return a literal decoded from data rather than PowerShell syntax."""
    encoded = base64.b64encode(value.encode()).decode("ascii")
    return (
        "[Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{encoded}'))"
    )


def process_command(pid: int) -> list[str]:
    """Inspect one process by numeric id, retaining command text internally."""
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
        "if($null -eq $p){exit 3}; "
        "@{pid=[int]$p.ProcessId;cmd=$p.CommandLine;exe=$p.ExecutablePath;"
        "parent=[int]$p.ParentProcessId;"
        "created=$p.CreationDate.ToUniversalTime().ToString('o')}"
        "|ConvertTo-Json -Compress"
    )
    return powershell(script)


def parse_process(data: bytes) -> dict[str, object]:
    """Require the exact shape produced by numeric process inspection."""
    value: object = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("invalid process inspection")
    record = cast(dict[str, object], value)
    if set(record) != {"pid", "cmd", "exe", "parent", "created"}:
        raise ValueError("invalid process inspection")
    return record


# The scheduler leaves out elements that hold their schema default when it
# stores a definition, so a missing element means that default value.
_TASK_DEFAULTS = {
    "t:Principals/t:Principal/t:RunLevel": "LeastPrivilege",
    "t:Settings/t:MultipleInstancesPolicy": "IgnoreNew",
    "t:Settings/t:Enabled": "true",
    "t:Triggers/t:LogonTrigger/t:Enabled": "true",
}


def task_mismatch(expected: ET.Element, actual: ET.Element) -> int | None:
    """Find the first way a stored task differs from the owned definition.

    Args:
        expected: The privately kept definition.
        actual: The definition as the scheduler reports it.

    Returns:
        The 1-based number of the first failing check, or None when the
        stored task is the owned one.
    """
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    fields = (
        "t:Principals/t:Principal/t:UserId",
        "t:Principals/t:Principal/t:LogonType",
        "t:Principals/t:Principal/t:RunLevel",
        "t:Actions/t:Exec/t:Command",
        "t:Actions/t:Exec/t:Arguments",
        "t:Settings/t:AllowHardTerminate",
        "t:Settings/t:ExecutionTimeLimit",
        "t:Settings/t:MultipleInstancesPolicy",
        "t:Settings/t:StopIfGoingOnBatteries",
        "t:Settings/t:DisallowStartIfOnBatteries",
        "t:Settings/t:StartWhenAvailable",
        "t:Settings/t:Enabled",
        "t:Settings/t:RestartOnFailure/t:Interval",
        "t:Settings/t:RestartOnFailure/t:Count",
        "t:Triggers/t:LogonTrigger/t:Enabled",
        "t:Triggers/t:LogonTrigger/t:UserId",
    )
    if (
        len(actual.findall("t:Actions/*", ns)) != 1
        or len(actual.findall("t:Principals/*", ns)) != 1
        or len(actual.findall("t:Triggers/*", ns)) != 1
    ):
        return 1
    actions = actual.find("t:Actions", ns)
    wanted = expected.find("t:Actions", ns)
    if (
        actions is None
        or wanted is None
        or actions.get("Context") != wanted.get("Context")
    ):
        return 2
    for number, path in enumerate(fields, start=3):
        default = _TASK_DEFAULTS.get(path)
        if actual.findtext(path, default, ns) != expected.findtext(
            path, default, ns
        ):
            return number
    return None


def task_matches(expected: ET.Element, actual: ET.Element) -> bool:
    """Require one owned user principal and one safe action with exact args.

    Args:
        expected: The privately kept definition.
        actual: The definition as the scheduler reports it.

    Returns:
        True when the stored task is the owned definition.
    """
    return task_mismatch(expected, actual) is None


def _private_json(path: Path) -> dict[str, object]:
    """Read bounded service metadata through the validated opened handle."""
    with platform_io.open_regular(path, root=path.parent) as handle:
        platform_io.assert_private_fd(handle.fileno())
        raw = handle.read(4097)
    value: object = json.loads(raw)
    if len(raw) > 4096 or not isinstance(value, dict):
        raise ValueError("invalid service metadata")
    return cast(dict[str, object], value)


def service_status(
    home: Path,
    env: Mapping[str, str],
    run: Callable[[Sequence[object]], subprocess.CompletedProcess[bytes]],
) -> tuple[bool | None, str | int]:
    """Verify the real service and heartbeat, returning only codes and IDs."""
    try:
        if sys.platform == "win32":
            return _windows_status(home, run)
        return _linux_status(home, env, run)
    except (OSError, ValueError, ET.ParseError, RecursionError):
        return None, "service_state_unknown"


def _windows_status(
    home: Path,
    run: Callable[[Sequence[object]], subprocess.CompletedProcess[bytes]],
) -> tuple[bool | None, str | int]:
    """Verify the owned task action and its actual pinned writer process."""
    base = home.parent
    metadata = _private_json(base / "lib/service.json")
    identifier = metadata.get("id")
    if metadata.get("backend") != "task_scheduler" or not isinstance(
        identifier, str
    ):
        return None, "service_metadata_unknown"
    with platform_io.open_regular(base / "task.xml", root=base) as handle:
        platform_io.assert_private_fd(handle.fileno())
        expected = ET.fromstring(handle.read(16385))
    queried = run(["schtasks.exe", "/Query", "/TN", identifier, "/XML"])
    if queried.returncode or not task_matches(
        expected, parse_task_query(queried.stdout)
    ):
        return False, "task_definition_mismatch"
    status = read_status(home)
    pid = status.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("writer_identity_missing")
    inspected = run(process_command(pid))
    if inspected.returncode:
        return False, "writer_not_running"
    process = parse_process(inspected.stdout)
    selected = read_selection(base)
    executable, command = process["exe"], process["cmd"]
    if (
        selected is None
        or not isinstance(executable, str)
        or not isinstance(command, str)
    ):
        return None, "writer_release_unknown"
    if (
        executable.casefold() != str(selected[1]).casefold()
        or str(selected[0]) not in command
    ):
        return False, "writer_release_mismatch"
    return True, pid


def parse_task_query(raw: bytes) -> ET.Element:
    """Parse ``schtasks /Query /XML`` output whatever its real encoding.

    The tool declares UTF-16 but writes console-code-page bytes when piped,
    so the declaration is dropped and the bytes decoded by their own marks.

    Args:
        raw: The raw standard output of the query.

    Returns:
        The root element of the task definition.
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
    return ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text))


def _linux_status(
    home: Path,
    env: Mapping[str, str],
    run: Callable[[Sequence[object]], subprocess.CompletedProcess[bytes]],
) -> tuple[bool | None, str | int]:
    """Verify the actual user service PID and its pinned command."""
    lib = Path(env.get("HOME") or Path.home()) / ".local/lib/muninn"
    metadata = _private_json(lib / "service.json")
    if metadata != {"backend": "systemd", "id": "muninn.service"}:
        return None, "service_metadata_unknown"
    return obs_linux_service.inspect(home, env, run)


def release_leftovers(env: Mapping[str, str]) -> list[str]:
    """List only superseded release IDs in the native private library."""
    lib = (
        windows_base(env) / "lib"
        if sys.platform == "win32"
        else Path(env.get("HOME") or Path.home()) / ".local/lib/muninn"
    )
    try:
        return sorted(path.name for path in lib.glob(".pruning-*"))
    except OSError:
        return []
