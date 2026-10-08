"""Time actual launcher stages after an authoritative cold hook failure."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from install import configedit
from install.context import Ctx
from muninn import platform_windows
from tests.ingest_support import ROOT

_PREFIX = b"__MUNINN_STAGE__"
_MARKERS = (
    ("    $native = Initialize-Native", "initialize"),
    ("        Hold-Directories $root", "ancestry"),
    (
        "        $recorded = Assert-InterpreterField $record.python",
        "selection",
    ),
    (
        "    if (-not $python) { throw 'No Python 3.13 or newer found; "
        "set MUNINN_PYTHON' }",
        "interpreter",
    ),
    ("    $code = $process.ExitCode", "cli"),
)
_KEYS = frozenset("stage_" + name + "_ms" for _, name in _MARKERS)


def timed_launcher(source: str) -> str:
    """Add finite cumulative markers without changing any launcher guard.

    Args:
        source: Tracked launcher copied into the synthetic bin directory.

    Returns:
        Same launcher with numeric stopwatch markers before normal exit.

    Raises:
        ValueError: If a known source boundary is missing or ambiguous.
    """
    result = (
        "$diagnosticClock = [Diagnostics.Stopwatch]::StartNew()\n"
        "$diagnosticTimes = [ordered]@{}\n" + source
    )
    for anchor, name in _MARKERS:
        if result.count(anchor) != 1:
            raise ValueError("unknown native launcher stage boundary")
        result = result.replace(
            anchor,
            anchor
            + "\n$diagnosticTimes['stage_"
            + name
            + "_ms'] = $diagnosticClock.ElapsedMilliseconds",
            1,
        )
    anchor = "    exit $code"
    if result.count(anchor) != 1:
        raise ValueError("unknown native launcher exit boundary")
    return result.replace(
        anchor,
        "    [Console]::Error.WriteLine('__MUNINN_STAGE__' + "
        "($diagnosticTimes | ConvertTo-Json -Compress))\n" + anchor,
        1,
    )


def parse_stages(raw: bytes) -> dict[str, int]:
    """Accept only one complete fixed numeric stage record from stderr.

    Args:
        raw: Captured synthetic process stderr; other bodies are discarded.

    Returns:
        Complete finite cumulative millisecond measurements.

    Raises:
        ValueError: If oversized, missing, malformed or unknown fields appear.
    """
    if len(raw) > 4096:
        raise ValueError("native stage record exceeds size bound")
    rows = [
        line[len(_PREFIX) :]
        for line in raw.splitlines()
        if line.startswith(_PREFIX)
    ]
    if len(rows) != 1:
        raise ValueError("native stage record is unavailable")
    values = json.loads(rows[0])
    if not isinstance(values, dict) or set(values) != _KEYS:
        raise ValueError("unknown native stage record")
    if not all(type(value) is int and value >= 0 for value in values.values()):
        raise ValueError("nonnumeric native stage record")
    return values


def launcher_stages(
    ctx: Ctx, env: dict[str, str], argv: str | list[str], payload: bytes
) -> dict[str, int]:
    """Run a fresh actual-source sibling with the same synthetic hook input.

    Args:
        ctx: Existing synthetic installed layout, including selected release.
        env: Same isolated provider environment used by the failed route.
        argv: Actual failed native PowerShell argument vector.
        payload: Same synthetic compact hook stdin.

    Returns:
        Only cumulative stage times, process exit and a fixed error indicator.
    """
    if sys.platform != "win32":
        return {}
    metrics: dict[str, int] = {"stage_error": 1}
    try:
        if not isinstance(argv, list):
            raise ValueError("native stage probe requires a structured route")
        launcher = ctx.muninn.with_suffix(".ps1")
        if argv.count(str(launcher)) != 1:
            raise ValueError("native stage probe cannot bind the launcher")
        platform_windows.assert_executable(Path(argv[0]))
        target = launcher.with_name("muninn-stage-proof.ps1")
        source = timed_launcher(
            (ROOT / "bin/muninn.ps1").read_text(encoding="utf-8")
        )
        configedit.atomic_write(target, source.encode("utf-8"), 0o600)
        command = [
            str(target) if item == str(launcher) else item for item in argv
        ]
        started = time.monotonic()
        done = subprocess.run(
            command,
            env=env,
            input=payload,
            cwd=ctx.home,
            capture_output=True,
            timeout=90,
            check=False,
        )
        metrics["stage_total_ms"] = round((time.monotonic() - started) * 1000)
        metrics["stage_returncode"] = done.returncode
        metrics.update(parse_stages(done.stderr))
        metrics["stage_error"] = int(done.returncode != 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return metrics
