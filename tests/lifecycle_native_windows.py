"""Run real installer smoke in a LeastPrivilege Task Scheduler child."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

from install.context import Ctx, run_real
from install.lifecycle import (
    _same_process,
    _task_engines,
    task_bytes,
    task_xml,
)
from muninn.obs_service import literal, powershell
from tests.native_diagnostics import transfer, write


def run_child(parent: Path, root: Path) -> dict[str, object]:
    """Create one scoped filtered-token task and await its terminal result."""
    identity = _grant_synthetic_access(parent)
    ctx = Ctx(parent, run_real, "outer")
    definition = ET.fromstring(
        task_xml(ctx, identity, Path(sys.executable), root)
    )
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    action = definition.find("t:Actions/t:Exec", ns)
    assert action is not None
    command = action.find("t:Command", ns)
    arguments = action.find("t:Arguments", ns)
    assert command is not None and arguments is not None
    command.text = sys.executable
    code = (
        "import os,sys;root=sys.argv.pop(1);"
        "scratch=sys.argv.pop(1);os.environ['RUNNER_TEMP']=scratch;"
        "os.environ['MUNINN_NATIVE_SCRATCH']=scratch;sys.path.insert(0,root);"
        "from tests.lifecycle_native import main;raise SystemExit(main())"
    )
    args = [
        "-I",
        "-B",
        "-X",
        "utf8",
        "-c",
        code,
        str(root),
        str(parent),
        "--child",
        str(parent),
    ]
    arguments.text = subprocess.list2cmdline(args)
    triggers = definition.find("t:Triggers", ns)
    settings = definition.find("t:Settings", ns)
    assert triggers is not None and settings is not None
    triggers.clear()
    restart = settings.find("t:RestartOnFailure", ns)
    assert restart is not None
    settings.remove(restart)
    xml = parent / "outer.xml"
    xml.write_bytes(task_bytes(definition))
    name = "Muninn-CI-" + uuid.uuid4().hex
    ctx.target = name
    write("lifecycle", "outer_task_create")
    _create_task(name, xml)
    write("lifecycle", "outer_task_run")
    subprocess.run(
        ["schtasks.exe", "/Run", "/TN", name],
        capture_output=True,
        check=True,
        timeout=30,
    )
    deadline = time.monotonic() + 600
    result = parent / "result.json"
    while not result.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "ordinary child timed out; tasks and state retained"
            )
        time.sleep(1)
    raw: object = json.loads(result.read_text())
    assert isinstance(raw, dict)
    report = cast(dict[str, object], raw)
    transfer("lifecycle", parent)
    if report.get("ok") is not True:
        raise RuntimeError("ordinary child lifecycle failed; state retained")
    assert report.get("elevated") is False, report
    pid, created = report.get("child_pid"), report.get("child_created")
    assert isinstance(pid, int) and isinstance(created, str)
    child = {"pid": pid, "created": created}
    while _task_engines(ctx) or _same_process(ctx, child):
        if time.monotonic() >= deadline:
            raise RuntimeError("outer child exit unknown; state retained")
        time.sleep(0.5)
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", name, "/F"],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return report


def _grant_synthetic_access(parent: Path) -> str:
    """Grant only the actual trusted user on synthetic CI prerequisites."""
    identity = (
        subprocess.run(
            powershell(
                "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
            ),
            capture_output=True,
            check=True,
            timeout=30,
        )
        .stdout.decode()
        .strip()
    )
    for path in (parent, Path(sys.executable).parent):
        subprocess.run(
            [
                "icacls",
                str(path),
                "/grant",
                f"*{identity}:(OI)(CI)(F)",
                "/T",
                "/Q",
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
    return identity


def registration_codes(data: bytes) -> dict[str, str | int]:
    """Extract fixed scheduler XML refusal codes without arbitrary messages."""
    text = data.decode(errors="replace")
    result: dict[str, str | int] = {"code": "task_registration_unknown"}
    if "task XML contains" in text:
        result["code"] = "task_xml_refused"
    elif "Access is denied" in text:
        result["code"] = "task_access_denied"
    match = re.search(r"\((\d{1,5}),(\d{1,5})\):([A-Za-z_:]{1,64})", text)
    if match is not None:
        result["xml_line"], result["xml_column"] = int(match[1]), int(match[2])
        element = match[3].rstrip(":").split(":")[-1]
        if element in {
            "Task",
            "Triggers",
            "Principals",
            "Principal",
            "Actions",
            "Exec",
            "Settings",
            "LogonType",
            "RunLevel",
            "ExecutionTimeLimit",
        }:
            result["xml_element"] = element
    return result


def _create_task(name: str, xml: Path) -> None:
    """Register the synthetic task, reporting only fixed refusal codes."""
    codes = xml_validation_codes(xml)
    write("lifecycle", None, metrics=codes)
    if codes.get("hresult") != 0:
        raise RuntimeError("original task XML validation refused")
    created_task = subprocess.run(
        ["schtasks.exe", "/Create", "/TN", name, "/XML", str(xml), "/F"],
        capture_output=True,
        timeout=30,
    )
    if created_task.returncode:
        codes = xml_validation_codes(xml)
        write(
            "lifecycle",
            None,
            metrics={"returncode": created_task.returncode, **codes},
        )
        print(
            json.dumps(
                registration_codes(created_task.stdout + created_task.stderr)
            ),
            flush=True,
        )
        created_task.check_returncode()


def xml_validation_codes(xml: Path) -> dict[str, int]:
    """Validate the exact XML in COM, exposing only numeric failure results."""
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=New-Object -ComObject Schedule.Service;"
        "$s.Connect();"
        f"$raw=[IO.File]::ReadAllText({literal(str(xml))});"
        "$body=$raw -replace '^<\\?xml[^>]*>\\s*','';"
        "$variants=[ordered]@{hresult=$raw;"
        'xml_utf8_hresult=(\'<?xml version="1.0" encoding="utf-8"?>\'+$body);'
        'xml_utf16_hresult=(\'<?xml version="1.0" '
        'encoding="utf-16"?>\'+$body);'
        "xml_omitted_hresult=$body};$codes=@{};"
        "foreach($key in $variants.Keys){"
        "try{$t=$s.NewTask(0);$t.XmlText=$variants[$key];$codes[$key]=0}"
        "catch{$e=$_.Exception;"
        "while($null -ne $e.InnerException){$e=$e.InnerException};"
        "$codes[$key]=[int]$e.HResult}};"
        "$codes['inner_hresult']=$codes['hresult'];"
        "$codes|ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        powershell(script), capture_output=True, timeout=30
    )
    try:
        value: object = json.loads(result.stdout)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: code
        for key, code in value.items()
        if key
        in {
            "hresult",
            "inner_hresult",
            "xml_utf8_hresult",
            "xml_utf16_hresult",
            "xml_omitted_hresult",
        }
        and isinstance(code, int)
        and not isinstance(code, bool)
    }
