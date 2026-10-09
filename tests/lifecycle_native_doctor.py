"""Fixed-code native doctor diagnostics taken before installer rollback."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from install.context import run_real
from muninn import obs_linux_service, obs_status

_CODES = frozenset(
    {
        "service_state_unknown",
        "service_metadata_unknown",
        "unit_definition_mismatch",
        "unit_registration_mismatch",
        "writer_not_running",
        "writer_identity_mismatch",
        "writer_release_mismatch",
        "writer_command_mismatch",
        "writer_identity_changed",
        "unit_definition_unsafe",
        "unit_definition_too_large",
        "release_selection_invalid",
        "release_directory_unsafe",
        "recorded_interpreter_missing",
        "recorded_interpreter_invalid",
        "recorded_interpreter_unsafe",
        "process_field_too_large",
        "process_identity_invalid",
        "writer_owner_mismatch",
        "process_command_invalid",
        "service_query_failed",
        "service_query_unknown",
    }
)


def failure_codes(stage: str, error: Exception) -> dict[str, str | int]:
    """Reduce an exception to its class, numeric error and known literal."""
    result: dict[str, str | int] = {stage + "_error": type(error).__name__}
    identifier = str(error)
    result[stage + "_code"] = identifier if identifier in _CODES else "unknown"
    if isinstance(error, OSError) and isinstance(error.errno, int):
        result[stage + "_errno"] = error.errno
    return result


def interpreter_codes(user: Path, uid: int) -> dict[str, int]:
    """Inspect only the refused synthetic executable's ordinary mode fields."""
    executable = (user / ".local/lib/muninn/python").readlink()
    metadata = executable.stat()
    return {
        "interpreter_regular": int(stat.S_ISREG(metadata.st_mode)),
        "interpreter_write_mask": stat.S_IMODE(metadata.st_mode) & 0o022,
        "interpreter_owner_trusted": int(metadata.st_uid in (uid, 0)),
        "interpreter_parent_write_mask": stat.S_IMODE(
            executable.parent.stat().st_mode
        )
        & 0o022,
    }


def codes(
    home: Path,
    env: Mapping[str, str],
    result: subprocess.CompletedProcess[bytes],
) -> dict[str, str | int]:
    """Inspect the actual doctor command's isolated native registration."""
    report: dict[str, str | int] = {
        "code": "native_doctor_boundary",
        "returncode": result.returncode,
        "doctor_code": "unknown",
        "doctor_ok": -1,
    }
    try:
        value = json.loads(result.stdout[:65536])
        for entry in value.get("checks", []):
            if entry.get("check") != "managed_service":
                continue
            detail = entry.get("detail")
            report["doctor_code"] = detail if detail in _CODES else "unknown"
            ok = entry.get("ok")
            report["doctor_ok"] = 1 if ok is True else 0 if ok is False else -1
    except (ValueError, AttributeError, TypeError):
        pass

    def run(argv: Sequence[object]) -> subprocess.CompletedProcess[bytes]:
        return run_real([str(value) for value in argv], env=env)

    try:
        query = obs_linux_service.query_unit(run)
        report["query_key_count"] = len(query)
        report["dropins_empty"] = int(query["DropInPaths"] == "")
        report["unit_active"] = int(query["ActiveState"] == "active")
        report["pid_matches"] = int(
            query["MainPID"] == str(obs_status.read_status(home).get("pid"))
        )
    except (OSError, ValueError) as exc:
        report.update(failure_codes("query", exc))
    try:
        ok, detail = obs_linux_service.inspect(home, env, run)
        report["inspect_ok"] = 1 if ok is True else 0 if ok is False else -1
        report["inspect_code"] = detail if detail in _CODES else "unknown"
    except (OSError, ValueError) as exc:
        report.update(failure_codes("inspect", exc))
        if (
            sys.platform == "linux"
            and str(exc) == "recorded_interpreter_unsafe"
        ):
            try:
                report.update(
                    interpreter_codes(Path(env["HOME"]), os.getuid())
                )
            except OSError as metadata_error:
                report.update(
                    failure_codes("interpreter_metadata", metadata_error)
                )
    return report


def run(
    argv: Sequence[str | Path],
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Return the real result unchanged, emitting only Linux doctor codes."""
    result = run_real(argv, env=env, input=input)
    if sys.platform == "linux" and argv and str(argv[-1]) == "doctor":
        variables = env or {}
        home = Path(variables["MUNINN_HOME"])
        print(json.dumps(codes(home, variables, result)), flush=True)
    return result
