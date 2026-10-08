"""Run real installer smoke in a LeastPrivilege Task Scheduler child."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

from install.context import Ctx, run_real
from install.lifecycle import _same_process, _task_engines, task_xml
from muninn.obs_service import powershell


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
        "import sys;sys.path.insert(0,sys.argv.pop(1));"
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
    xml.write_bytes(
        ET.tostring(definition, encoding="utf-8", xml_declaration=True)
    )
    name = "Muninn-CI-" + uuid.uuid4().hex
    ctx.target = name
    subprocess.run(
        ["schtasks.exe", "/Create", "/TN", name, "/XML", str(xml), "/F"],
        capture_output=True,
        check=True,
        timeout=30,
    )
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
    if report.get("ok") is not True:
        raise RuntimeError(str(report))
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
