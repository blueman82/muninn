"""Reduce actual installer command failures to finite qualification codes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import FrameType

from install.context import StepFailedError, must

COMMAND_FAMILIES = {
    "git": 1,
    "git.exe": 1,
    "powershell.exe": 2,
    "pwsh": 2,
    "pwsh.exe": 2,
    "python": 3,
    "python.exe": 3,
    "python3": 3,
    "python3.13": 3,
    "codex": 4,
    "codex.exe": 4,
    "codex.cmd": 4,
    "muninn": 5,
    "muninn.cmd": 5,
    "systemctl": 6,
    "launchctl": 7,
    "ps": 8,
    "schtasks.exe": 9,
}
TASK_ACTIONS = {
    "/create": 10,
    "/run": 11,
    "/end": 12,
    "/query": 13,
    "/delete": 14,
}
CALLSITES = {
    ("install.preflight", "_git"): 1,
    ("install.snapshot", "release_schema"): 2,
    ("install.snapshot", "start_again"): 3,
    ("install.legacy_exclusion", "stop_service"): 4,
    ("install.legacy_baseline", "candidate"): 5,
    ("install.writer_chain", "_inspect"): 6,
    ("install.rollback", "<lambda>"): 7,
    ("install.steps_config", "_add_plugin"): 8,
    ("install.lifecycle", "_identity"): 9,
    ("install.lifecycle", "preflight_service"): 10,
    ("install.lifecycle", "_task_owned"): 11,
    ("install.lifecycle", "_task_engines"): 12,
    ("install.lifecycle", "_stop_windows"): 13,
    ("install.lifecycle", "stop"): 14,
    ("install.lifecycle", "start"): 15,
    ("install.lifecycle", "remove"): 16,
    ("install.steps_release", "prepare_release"): 17,
    ("install.steps_release", "restart"): 18,
    ("install.steps_release", "start_new"): 19,
    ("tests.lifecycle_native_windows", "hosted_identity"): 20,
}
FAILURE_BOUNDS = {
    "step_command_family": (0, 14),
    "step_callsite": (0, 20),
    "step_exit_status": (-2147483648, 4294967295),
}


def _family(argv: object) -> int:
    """Map known executable names and task verbs without keeping arguments."""
    if not isinstance(argv, (list, tuple)) or not argv:
        return 0
    executable = argv[0]
    if not isinstance(executable, (str, Path)):
        return 0
    name = str(executable).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    family = COMMAND_FAMILIES.get(name, 0)
    if family == 9 and len(argv) > 1 and isinstance(argv[1], str):
        return TASK_ACTIONS.get(argv[1].casefold(), family)
    return family


def _callsite(frame: FrameType | None) -> int:
    """Map only known repository caller identities to a fixed numeric code."""
    if frame is None:
        return 0
    module = frame.f_globals.get("__name__")
    if not isinstance(module, str):
        return 0
    return CALLSITES.get((module, frame.f_code.co_name), 0)


def failure_codes(error: Exception) -> dict[str, int]:
    """Extract the real nonzero result only from the exact must traceback.

    Args:
        error: Caught child exception whose private message stays local.

    Returns:
        Finite command and caller codes plus the actual numeric exit status,
        or no fields when that boundary is unavailable. Code zero means an
        unrecognised command family or caller, never a guessed diagnosis.
    """
    if not isinstance(error, StepFailedError):
        return {}
    previous: FrameType | None = None
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        if frame.f_code is must.__code__:
            result = frame.f_locals.get("r")
            if (
                isinstance(result, subprocess.CompletedProcess)
                and type(result.returncode) is int
                and result.returncode != 0
                and -2147483648 <= result.returncode <= 4294967295
            ):
                return {
                    "step_command_family": _family(frame.f_locals.get("argv")),
                    "step_callsite": _callsite(previous),
                    "step_exit_status": result.returncode,
                }
            return {}
        previous = frame
        trace = trace.tb_next
    return {}
