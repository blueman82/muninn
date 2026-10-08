"""Per-user native services with graceful, verified writer quiescence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

from install import configedit as ce
from install.context import Ctx, StepFailedError, job, must, wait
from install.release_io import write_private
from muninn import obs_status, platform_io, platform_windows, poller_stop
from muninn.obs_linux_service import (
    query_unit,
    read_unit,
    selection,
    unit_matches,
)
from muninn.obs_service import (
    literal,
    parse_process,
    powershell,
    process_command,
    task_matches,
)
from muninn.obs_service_commands import render_unit
from muninn.platform_paths import read_selection

_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_NS = {"t": _NAMESPACE}
_SID = re.compile(r"S-1-\d+(?:-\d+){1,15}\Z")


def linux_unit(ctx: Ctx, python: Path, release: Path) -> str:
    """Render a user unit whose stop cannot terminate an open transaction."""
    env = {
        "HOME": str(ctx.home),
        "MUNINN_HOME": str(ctx.data),
        "CODEX_HOME": str(ctx.codex_home),
        "CLAUDE_CONFIG_DIR": str(ctx.settings.parent),
    }
    if library := os.environ.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = library
    return render_unit(python, release, ctx.data, env)


def task_xml(ctx: Ctx, sid: str, python: Path, release: Path) -> bytes:
    """Render a task executed as the interactive user without elevation."""
    root = ET.Element("Task", {"xmlns": _NAMESPACE, "version": "1.2"})
    registration = ET.SubElement(root, "RegistrationInfo")
    ET.SubElement(registration, "Author").text = sid
    triggers = ET.SubElement(root, "Triggers")
    logon = ET.SubElement(triggers, "LogonTrigger")
    ET.SubElement(logon, "Enabled").text = "true"
    ET.SubElement(logon, "UserId").text = sid
    principals = ET.SubElement(root, "Principals")
    principal = ET.SubElement(principals, "Principal", {"id": "User"})
    for key, value in (
        ("UserId", sid),
        ("LogonType", "InteractiveToken"),
        ("RunLevel", "LeastPrivilege"),
    ):
        ET.SubElement(principal, key).text = value
    settings = ET.SubElement(root, "Settings")
    for key, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "false"),
        ("StartWhenAvailable", "true"),
        ("Enabled", "true"),
        ("ExecutionTimeLimit", "PT0S"),
    ):
        ET.SubElement(settings, key).text = value
    retry = ET.SubElement(settings, "RestartOnFailure")
    ET.SubElement(retry, "Interval").text = "PT1M"
    ET.SubElement(retry, "Count").text = "3"
    actions = ET.SubElement(root, "Actions", {"Context": "User"})
    action = ET.SubElement(actions, "Exec")
    action_argv = task_action(ctx, python)
    ET.SubElement(action, "Command").text = action_argv[0]
    ET.SubElement(action, "Arguments").text = " ".join(action_argv[1:])
    return task_bytes(root)


def task_bytes(definition: ET.Element) -> bytes:
    """Serialize task XML with a BOM and declaration matching COM strings."""
    return ET.tostring(definition, encoding="utf-16", xml_declaration=True)


def _identity(ctx: Ctx) -> str:
    """Require an ordinary user token before creating native private state."""
    script = (
        "$i=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "$p=[Security.Principal.WindowsPrincipal]::new($i);"
        "$a=[Security.Principal.WindowsBuiltInRole]::Administrator;"
        "@{sid=$i.User.Value;elevated=$p.IsInRole($a)}"
        "|ConvertTo-Json -Compress"
    )
    raw: object = json.loads(must(ctx, powershell(script), quiet=True).stdout)
    if not isinstance(raw, dict):
        raise StepFailedError("cannot establish ordinary user identity")
    value = cast(dict[str, object], raw)
    sid = value.get("sid")
    if (
        value.get("elevated") is not False
        or not isinstance(sid, str)
        or not _SID.fullmatch(sid)
    ):
        raise StepFailedError("run the installer as an ordinary Windows user")
    ctx.target = (
        "Muninn-"
        + hashlib.sha256(
            f"{sid}:{ctx.lib.parent}".casefold().encode()
        ).hexdigest()[:20]
    )
    return sid


def _linux_owned(ctx: Ctx) -> None:
    """Require the retained private unit and unchanged native registration."""
    current, python, _ = selection(ctx.home)
    registration = query_unit(ctx.run)
    if (
        registration["FragmentPath"] != str(ctx.plist)
        or registration["DropInPaths"]
    ):
        raise StepFailedError("the user service registration is not owned")
    if not unit_matches(
        read_unit(ctx.plist),
        python,
        current,
        ctx.data,
        {
            "HOME": str(ctx.home),
            "MUNINN_HOME": str(ctx.data),
            "CODEX_HOME": str(ctx.codex_home),
            "CLAUDE_CONFIG_DIR": str(ctx.settings.parent),
        },
    ):
        raise StepFailedError("the user service definition is not owned")


def preflight_service(ctx: Ctx) -> None:
    """Refuse unavailable or unsafe native lifecycle before any mutation."""
    if ctx.platform == "linux":
        must(ctx, ["systemctl", "--user", "show-environment"], quiet=True)
        if ctx.plist.exists():
            try:
                _linux_owned(ctx)
            except (OSError, ValueError) as exc:
                raise StepFailedError(
                    "cannot validate the owned user service"
                ) from exc
    elif ctx.platform == "win32":
        _identity(ctx)
        if sys.platform == "win32":
            platform_windows.assert_ancestry(ctx.lib)
            platform_windows.assert_executable(Path(sys.executable))
        if ctx.plist.exists():
            _task_owned(ctx)
        elif (
            ctx.run(
                ["schtasks.exe", "/Query", "/TN", ctx.target, "/XML"]
            ).returncode
            == 0
        ):
            raise StepFailedError(
                "the task name already exists without private ownership"
            )


def _task_owned(ctx: Ctx) -> ET.Element:
    """Validate registered critical settings against the private definition."""
    if not platform_io.is_private(ctx.plist):
        raise StepFailedError("task ownership definition is unsafe")
    with platform_io.open_regular(ctx.plist, root=ctx.lib.parent) as handle:
        platform_io.assert_private_fd(handle.fileno())
        expected = ET.fromstring(handle.read(16385))
    result = must(
        ctx, ["schtasks.exe", "/Query", "/TN", ctx.target, "/XML"], quiet=True
    )
    actual = ET.fromstring(result.stdout)
    if not task_matches(expected, actual):
        raise StepFailedError(
            "registered task definition is not the owned poller"
        )
    return actual


def _task_engines(ctx: Ctx) -> list[int]:
    """Read actual running instances, including disabled but live tasks."""
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=New-Object -ComObject Schedule.Service;"
        "$s.Connect();"
        f"$n={literal(ctx.target)};"
        "$t=$s.GetFolder('\\').GetTask($n);"
        "$ids=@($t.GetInstances(0)|ForEach-Object {[int]$_.EnginePID});"
        "ConvertTo-Json -InputObject $ids -Compress"
    )
    value: object = json.loads(
        must(ctx, powershell(script), quiet=True).stdout
    )
    if not isinstance(value, list):
        raise StepFailedError("scheduled task instances are unknown")
    values = cast(list[object], value)
    if any(
        not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
        for pid in values
    ):
        raise StepFailedError("scheduled task engine identity is unknown")
    return cast(list[int], values)


def _task_running(ctx: Ctx) -> bool:
    """Report actual running instances rather than the task enabled state."""
    return bool(_task_engines(ctx))


def _same_process(ctx: Ctx, process: dict[str, object]) -> bool:
    """Bind waits to process creation identity rather than a reused PID."""
    pid = process["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise StepFailedError("process identity is invalid")
    result = ctx.run(process_command(pid))
    if result.returncode == 3:
        return False
    if result.returncode:
        raise StepFailedError("cannot establish process quiescence")
    return parse_process(result.stdout)["created"] == process["created"]


def _stop_windows(ctx: Ctx) -> None:
    """Disable starts, bind writer and launcher identities, and await exit."""
    definition = _task_owned(ctx)
    must(
        ctx,
        ["schtasks.exe", "/Change", "/TN", ctx.target, "/DISABLE"],
        quiet=True,
    )
    enabled = definition.find("t:Settings/t:Enabled", _NS)
    assert enabled is not None
    enabled.text = "false"
    write_private(ctx.plist, task_bytes(definition))
    found = job(ctx)
    if found and (pid := found["pid"]):
        writer = parse_process(
            must(ctx, process_command(pid), quiet=True).stdout
        )
        parent = writer["parent"]
        if (
            not isinstance(parent, int)
            or isinstance(parent, bool)
            or parent <= 0
        ):
            raise StepFailedError("writer launcher identity is unavailable")
        launcher = parse_process(
            must(ctx, process_command(parent), quiet=True).stdout
        )
        command = definition.findtext(
            "t:Actions/t:Exec/t:Command", namespaces=_NS
        )
        args = definition.findtext(
            "t:Actions/t:Exec/t:Arguments", namespaces=_NS
        )
        selected = read_selection(ctx.lib.parent)
        executable, launcher_exe = writer["exe"], launcher["exe"]
        launcher_command = launcher["cmd"]
        if (
            selected is None
            or not isinstance(executable, str)
            or executable.casefold() != str(selected[1]).casefold()
            or str(selected[0]) not in found["cmd"]
            or not isinstance(launcher_exe, str)
            or not command
            or launcher_exe.casefold() != command.casefold()
            or not isinstance(launcher_command, str)
            or not args
            or args not in launcher_command
            or not (
                {launcher["pid"], launcher["parent"]} & set(_task_engines(ctx))
            )
        ):
            raise StepFailedError(
                "writer and launcher do not match the owned task"
            )
        status = obs_status.read_status(ctx.data)
        generation = status.get("stop_generation")
        if status.get("pid") != pid or not isinstance(generation, str):
            raise StepFailedError("writer stop generation is unavailable")
        if not _same_process(ctx, writer) or not _same_process(ctx, launcher):
            raise StepFailedError("writer or launcher changed before stop")
        poller_stop.request(ctx.data, pid, generation)
        wait(
            ctx,
            lambda: not _same_process(ctx, writer),
            30,
            "writer did not quiesce; retained state unchanged",
        )
        wait(
            ctx,
            lambda: not _same_process(ctx, launcher),
            30,
            "launcher did not quiesce; retained state unchanged",
        )
    wait(
        ctx,
        lambda: not _task_running(ctx),
        30,
        "task did not quiesce; retained state unchanged",
    )


def stop(ctx: Ctx) -> None:
    """Prevent automatic starts and stop the owned service without force."""
    if ctx.platform == "win32":
        if ctx.plist.exists():
            _stop_windows(ctx)
    elif ctx.platform == "linux":
        must(ctx, ["systemctl", "--user", "disable", ctx.target], quiet=True)
        must(
            ctx,
            ["systemctl", "--user", "stop", "--no-block", ctx.target],
            quiet=True,
        )
        wait(
            ctx,
            lambda: not ((found := job(ctx)) and found["pid"]),
            30,
            "writer did not quiesce; retained state unchanged",
        )
    else:
        ctx.run(["launchctl", "bootout", ctx.target])
        wait(ctx, lambda: job(ctx) is None, 30, "job still loaded")


def start(ctx: Ctx, python: Path, release: Path) -> None:
    """Publish and start an owned native per-user service definition."""
    backend = "systemd" if ctx.platform == "linux" else "task_scheduler"
    if ctx.platform == "win32":
        _identity(ctx)
    write_private(
        ctx.lib / "service.json",
        json.dumps({"backend": backend, "id": ctx.target}).encode(),
    )
    if ctx.platform == "linux":
        platform_io.ensure_private_dir(ctx.plist.parent)
        ce.atomic_write(
            ctx.plist, linux_unit(ctx, python, release).encode(), 0o600
        )
        must(ctx, ["systemctl", "--user", "daemon-reload"], quiet=True)
        must(ctx, ["systemctl", "--user", "enable", ctx.target], quiet=True)
        must(ctx, ["systemctl", "--user", "start", ctx.target], quiet=True)
    elif ctx.platform == "win32":
        sid = _identity(ctx)
        if ctx.plist.exists():
            _task_owned(ctx)
        ce.atomic_write(ctx.plist, task_xml(ctx, sid, python, release), 0o600)
        must(
            ctx,
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                ctx.target,
                "/XML",
                ctx.plist,
                "/F",
            ],
            quiet=True,
        )
        _task_owned(ctx)
        must(ctx, ["schtasks.exe", "/Run", "/TN", ctx.target], quiet=True)


def task_action(ctx: Ctx, python: Path) -> list[str]:
    """Run the proven stable launcher in the task's sole PowerShell process."""
    variables = {
        "HOME": str(ctx.home),
        "USERPROFILE": str(ctx.home),
        "LOCALAPPDATA": str(ctx.lib.parent.parent),
        "MUNINN_HOME": str(ctx.data),
        "MUNINN_PYTHON": str(python),
        "CODEX_HOME": str(ctx.codex_home),
        "CLAUDE_CONFIG_DIR": str(ctx.settings.parent),
    }
    script = "$ErrorActionPreference='Stop';"
    script += "".join(
        f"$env:{key}={literal(value)};" for key, value in variables.items()
    )
    script += "$env:MUNINN_ROOTS=$null;"
    script += f"$launcher={literal(str(ctx.muninn.with_suffix('.ps1')))};"
    script += "& $launcher serve --interval 60;exit $LASTEXITCODE"
    argv = powershell(script)
    return [
        argv[0],
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        argv[-1],
    ]


def remove(ctx: Ctx) -> None:
    """Remove only the verified owned backend after graceful quiescence."""
    stop(ctx)
    if ctx.platform == "win32" and ctx.plist.exists():
        _task_owned(ctx)
        must(ctx, ["schtasks.exe", "/Delete", "/TN", ctx.target, "/F"])
    if ctx.platform == "linux" and ctx.plist.exists():
        ctx.plist.unlink()
        must(ctx, ["systemctl", "--user", "daemon-reload"], quiet=True)
