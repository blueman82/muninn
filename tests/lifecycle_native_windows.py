"""Run real installer smoke in a LeastPrivilege Task Scheduler child."""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

from install import lifecycle
from install.context import Ctx, must, run_real
from install.lifecycle import (
    _SID,
    _same_process,
    _task_engines,
    task_bytes,
    task_xml,
)
from muninn.obs_service import literal, powershell, task_mismatch
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
    assert isinstance(report.get("elevated"), bool), report
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


def identity_codes(data: bytes) -> dict[str, int]:
    """Reduce the current identity response to SID shape and admin count."""
    value: object = json.loads(data)
    if not isinstance(value, dict) or not isinstance(
        value.get("elevated"), bool
    ):
        raise ValueError("identity probe shape unavailable")
    sid = value.get("sid")
    return {
        "sid_valid": int(
            isinstance(sid, str) and _SID.fullmatch(sid) is not None
        ),
        "admin_member": int(value["elevated"]),
    }


def token_codes() -> dict[str, int]:
    """Inspect this child token without changing privileges or launching it."""
    if sys.platform != "win32":
        raise OSError("native token probe requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    current = kernel.GetCurrentProcess
    current.argtypes, current.restype = [], ctypes.c_void_p
    close = kernel.CloseHandle
    close.argtypes, close.restype = [ctypes.c_void_p], wintypes.BOOL
    opened = security.OpenProcessToken
    opened.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    opened.restype = wintypes.BOOL
    query = security.GetTokenInformation
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    token = ctypes.c_void_p()
    if not opened(current(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        codes: dict[str, int] = {}
        size = wintypes.DWORD()
        for key, information in (
            ("elevation_type", 18),
            ("token_elevated", 20),
        ):
            value = wintypes.DWORD()
            if not query(
                token,
                information,
                ctypes.byref(value),
                ctypes.sizeof(value),
                ctypes.byref(size),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            codes[key] = int(value.value)
        linked = ctypes.c_void_p()
        available = bool(
            query(
                token,
                19,
                ctypes.byref(linked),
                ctypes.sizeof(linked),
                ctypes.byref(size),
            )
        )
        codes["linked_token_available"] = int(available)
        codes["linked_token_error"] = (
            0 if available else ctypes.get_last_error()
        )
        if available and linked.value:
            close(linked)
        response = subprocess.run(
            powershell(
                "$i=[Security.Principal.WindowsIdentity]::GetCurrent();"
                "$p=[Security.Principal.WindowsPrincipal]::new($i);"
                "$a=[Security.Principal.WindowsBuiltInRole]::Administrator;"
                "@{sid=$i.User.Value;elevated=$p.IsInRole($a)}"
                "|ConvertTo-Json -Compress"
            ),
            capture_output=True,
            check=True,
            timeout=30,
        )
        return codes | identity_codes(response.stdout)
    finally:
        close(token)


def hosted_identity(ctx: Ctx) -> str:
    """Return the runner SID where hosted CI has no limited token to offer.

    Hosted Windows runners run with UAC off, so the interactive user is an
    administrator with no filtered token. Only the elevation clause of the
    product guard is waived, and the parent records that the token was
    elevated.
    """
    script = "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
    sid = must(ctx, powershell(script), quiet=True).stdout.decode().strip()
    if not _SID.fullmatch(sid):
        raise RuntimeError("cannot establish runner identity")
    ctx.target = (
        "Muninn-"
        + hashlib.sha256(
            f"{sid}:{ctx.lib.parent}".casefold().encode()
        ).hexdigest()[:20]
    )
    return sid


def record_task_mismatch() -> list[int]:
    """Make the product ownership check also note which check failed.

    Returns:
        A list that receives the number of each failing check.
    """
    seen: list[int] = []

    def recording(expected: ET.Element, actual: ET.Element) -> bool:
        """Report the product verdict while keeping only the check number."""
        index = task_mismatch(expected, actual)
        if index is not None:
            seen.append(index)
        return index is None

    lifecycle.task_matches = recording
    return seen


def record_writer_mismatch() -> list[int]:
    """Make the product writer binding also note which check failed.

    Returns:
        A list that receives the number of each failing check.
    """
    seen: list[int] = []
    real = lifecycle.writer_mismatch

    def recording(*args: Any) -> int | None:
        """Pass the product verdict through while keeping the check number."""
        index = real(*args)
        if index is not None:
            seen.append(index)
        return index

    lifecycle.writer_mismatch = recording
    return seen
