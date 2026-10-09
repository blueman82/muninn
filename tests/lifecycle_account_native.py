"""CI-only ordinary-account and real InteractiveToken no-op topology probe."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from install.context import Ctx, run_real
from install.lifecycle import task_bytes, task_xml
from muninn import platform_io
from muninn.obs_service import literal, powershell
from tests.lifecycle_account_child import child_script
from tests.lifecycle_account_errors import failure_codes, response
from tests.lifecycle_account_scripts import outer_script
from tests.native_diagnostics import write


def noop_definition(parent: Path) -> bytes:
    """Reuse the product task safety settings with a fixed no-op action."""
    ctx = Ctx(parent, run_real, "noop", platform="win32")
    root = ET.fromstring(
        task_xml(ctx, "@SID@", Path("/unused"), Path("/unused"))
    )
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    action = root.find("t:Actions/t:Exec", ns)
    assert action is not None
    command = action.find("t:Command", ns)
    args = action.find("t:Arguments", ns)
    assert command is not None and args is not None
    destination = literal(str(parent / "task-result.json"))
    script = (
        "$i=[Security.Principal.WindowsIdentity]::GetCurrent();"
        "$p=[Security.Principal.WindowsPrincipal]::new($i);"
        "$a=[Security.Principal.WindowsBuiltInRole]::Administrator;"
        "$r=@{sid=$i.User.Value;admin=[int]$p.IsInRole($a);pid=$PID;"
        "created=[Diagnostics.Process]::GetCurrentProcess().StartTime.Ticks;"
        "session=[Diagnostics.Process]::GetCurrentProcess().SessionId};"
        f"[IO.File]::WriteAllText(({destination}),"
        "($r|ConvertTo-Json -Compress))"
    )
    argv = powershell(script)
    command.text, args.text = argv[0], " ".join(argv[1:])
    return task_bytes(root)


def check_result(report: Mapping[str, object]) -> None:
    """Require actual ordinary children and verified completed task exit."""
    wanted = {
        "ordinary_child_admin": 0,
        "scheduler_child_admin": 0,
        "scheduler_exit": 0,
        "scheduler_instances": 0,
        "interactive_recognized": 1,
        "account_retained": 0,
        "desktop_restored": 1,
        "private_desktop_created": 1,
    }
    if "winerror" in report or any(
        type(report.get(key)) is not int or report[key] != value
        for key, value in wanted.items()
    ):
        raise ValueError("ordinary_interactive_topology_unproven")
    sessions = (
        "caller_session",
        "token_session",
        "ordinary_child_session",
        "scheduler_child_session",
    )
    for key in sessions:
        value = report.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("ordinary_session_evidence_unproven")
    if (
        report["ordinary_child_session"] != report["caller_session"]
        or report["scheduler_child_session"]
        != report["ordinary_child_session"]
    ):
        raise ValueError("ordinary_session_identity_mismatch")


def main() -> int:
    """Probe only ephemeral native resources after provider cold routes."""
    if sys.platform != "win32":
        raise OSError("native_windows_required")
    temporary = tempfile.TemporaryDirectory(
        prefix="muninn-account-spike-", delete=False
    )
    parent = Path(temporary.name)
    platform_io.ensure_private_dir(parent)
    (parent / "task.xml").write_bytes(noop_definition(parent))
    (parent / "child.ps1").write_text(
        child_script(parent), encoding="utf-8-sig"
    )
    (parent / "outer.ps1").write_text(
        outer_script(parent), encoding="utf-8-sig"
    )
    write("ordinary", "account_create")
    try:
        argv = powershell("")
        result = subprocess.run(
            [
                argv[0],
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(parent / "outer.ps1"),
            ],
            capture_output=True,
            timeout=90,
        )
        report = response(result)
        phase = report.get("phase")
        if not isinstance(phase, str):
            raise ValueError("ordinary_phase_invalid")
        metrics = {
            key: value
            for key, value in report.items()
            if type(value) is int and key != "winerror"
        }
        error = report.get("winerror")
        diagnostic_error = (
            OSError(error, "native_account_spike_failed")
            if isinstance(error, int)
            else None
        )
        write(
            "ordinary",
            phase,
            error=diagnostic_error,
            metrics=cast(dict[str, int], metrics),
        )
        if result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, stderr=result.stderr
            )
        check_result(report)
    except Exception as exc:
        metrics = (
            failure_codes(exc.returncode, exc.stderr or b"")
            if isinstance(exc, subprocess.CalledProcessError)
            else {}
        )
        write("ordinary", None, error=exc, metrics=metrics)
        print(json.dumps({"retained_state": str(parent)}))
        raise
    temporary.cleanup()
    write("ordinary", "complete", completed=True)
    print(json.dumps({"ordinary_interactive_noop": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
