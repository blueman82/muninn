"""Small, code-only early artifacts for real native CI proof commands."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from muninn.file_sync import sync_fd
from muninn.platform_io import ensure_private_dir, open_private, open_regular

_ROOT = Path(__file__).resolve().parents[1]
_ARTIFACTS = frozenset(("provider", "lifecycle", "ordinary"))
_PHASES = frozenset(
    (
        "initial",
        "account_create",
        "account_logon",
        "account_child_start",
        "account_task_create",
        "account_task_run",
        "account_task_wait",
        "account_cleanup",
        "fixture_home",
        "fixture_data",
        "fixture_lib",
        "fixture_bin",
        "fixture_codex",
        "fixture_claude",
        "fixture_release",
        "fixture_copy",
        "fixture_selection",
        "fixture_ingest",
        "fixture_knowledge",
        "fixture_status",
        "codex_version",
        "codex_release",
        "codex_render",
        "security_probe",
        "codex_config",
        "codex_add",
        "codex_cache",
        "codex_trust",
        "codex_probe",
        "claude_render",
        "routes",
        "fresh_install",
        "fresh_stop",
        "upgrade_install",
        "upgrade_stop",
        "refused_cli",
        "busy_stop",
        "crash_restart",
        "rollback_upgrade",
        "outer_task_create",
        "outer_task_run",
        "ordinary_child_identity",
        "ordinary_child_start",
        "manager_start",
        "complete",
    )
)
_METRICS = frozenset(
    (
        "route",
        "elapsed_ms",
        "budget_ms",
        "returncode",
        "hooks_count",
        "codex_hooks",
        "hresult",
        "inner_hresult",
        "admin_member",
        "sid_valid",
        "elevation_type",
        "token_elevated",
        "linked_token_available",
        "linked_token_error",
        "stderr_bytes",
        "stderr_clixml",
        "stderr_progress_only",
        "stderr_error_count",
        "stderr_other_count",
        "python_pe_machine",
        "powershell_pe_machine",
        "baseline_python_ms",
        "baseline_python_returncode",
        "baseline_powershell_ms",
        "baseline_powershell_returncode",
        "baseline_addtype_ms",
        "baseline_addtype_returncode",
        "baseline_error",
        "baseline_emit_ms",
        "baseline_emit_returncode",
        "baseline_emit_error",
        "stage_initialize_ms",
        "stage_root_ms",
        "stage_installed_ms",
        "stage_first_before_hold_ms",
        "stage_first_ordinary_before_drive_ms",
        "stage_first_ordinary_drive_ms",
        "stage_first_ordinary_item_ms",
        "stage_first_open_ms",
        "stage_first_acl_enter_ms",
        "stage_first_identity_ms",
        "stage_first_translate_ms",
        "stage_first_acl_exit_ms",
        "stage_first_native_enter_ms",
        "stage_first_native_create_ms",
        "stage_first_native_metadata_ms",
        "stage_first_native_path_ms",
        "stage_first_hold_enter_ms",
        "stage_first_before_open_ms",
        "stage_ancestry_ms",
        "stage_selection_base_ms",
        "stage_selection_metadata_ms",
        "stage_selection_json_ms",
        "stage_selection_fields_ms",
        "stage_selection_release_ms",
        "stage_selection_ms",
        "stage_interpreter_ms",
        "stage_cli_ms",
        "stage_total_ms",
        "stage_returncode",
        "stage_error",
        "caller_quota_present",
        "caller_quota_enabled",
        "caller_assign_present",
        "caller_assign_enabled",
        "caller_impersonate_present",
        "caller_impersonate_enabled",
        "ordinary_child_admin",
        "scheduler_child_admin",
        "scheduler_exit",
        "scheduler_instances",
        "interactive_recognized",
        "account_retained",
        "desktop_restored",
        "station_restore_ok",
        "station_restore_error",
        "desktop_restore_ok",
        "desktop_restore_error",
        "station_identity_ok",
        "desktop_identity_ok",
        "desktop_identity_before",
        "desktop_restore_called",
        "child_stage",
        "child_launch_ok",
        "child_wait_result",
        "child_exit_query_ok",
        "child_exit_code",
        "child_module_readable",
        "child_module_hresult",
        "child_error_category",
        "child_command_type",
        "child_parse_count",
        "child_parse_line",
        "child_report_seen",
        "child_failure_stage",
        "child_failure_hresult",
        "child_exception_kind",
        "child_body_hresult",
        "child_body_line",
        "task_principal_sid_equal",
        "task_principal_logon_type",
        "task_principal_run_level",
        "task_owned_mismatch",
        "writer_binding_mismatch",
        "step_failure_id",
        "new_job_flags",
        "doctor_output_length",
        "doctor_output_first_byte",
        "task_last_result",
        "task_state",
        "child_failure_line",
        "private_desktop_created",
        "token_session",
        "caller_session",
        "ordinary_child_session",
        "scheduler_child_session",
        "outer_returncode",
        "outer_compile_code",
        "outer_parser_error",
        "outer_stage",
        "xml_utf8_hresult",
        "xml_utf16_hresult",
        "xml_omitted_hresult",
    )
)
_PROBE_METRICS = (
    frozenset({"probe_original_control"})
    | frozenset(
        f"probe_v{variant}_{stage}_{component}"
        for variant in (1, 2, 3)
        for stage in ("initial", "final")
        for component in (
            "control",
            "owner_equal",
            "group_equal",
            "dacl_equal",
        )
    )
    | frozenset(f"probe_v{variant}_error" for variant in (1, 2, 3))
)
_METRICS = _METRICS | _PROBE_METRICS
_METRIC_BOUNDS = {
    "child_exception_kind": (-1, 5),
    "child_body_hresult": (-2147483648, 2147483647),
    "child_body_line": (0, 4096),
    "task_principal_sid_equal": (0, 1),
    "task_principal_logon_type": (0, 6),
    "task_principal_run_level": (0, 1),
    "task_owned_mismatch": (0, 32),
    "writer_binding_mismatch": (0, 16),
    "step_failure_id": (0, 2147483647),
    "new_job_flags": (0, 7),
    "doctor_output_length": (0, 65535),
    "doctor_output_first_byte": (0, 255),
    "task_last_result": (-2147483648, 2147483647),
    "task_state": (0, 4),
}
_SECURITY_CODES = frozenset(
    (
        "control_before",
        "control_after",
        "owner_equal",
        "group_equal",
        "dacl_equal",
    )
)
_CODES = (
    frozenset(("errno", "winerror", "cause_winerror", "error_line"))
    | _SECURITY_CODES
)


def _previous(path: Path) -> dict[str, str | int]:
    """Recover only safe previous phase, code, count and source identifiers."""
    if not path.exists():
        return {}
    with open_regular(path) as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError("native diagnostic exceeds its size bound")
    values = json.loads(raw)
    if not isinstance(values, dict):
        raise ValueError("native diagnostic must be an object")
    return {
        key: value for key, value in values.items() if _safe_field(key, value)
    }


def _safe_field(key: str, value: object) -> bool:
    """Accept only explicit scalar fields when exporting a child artifact."""
    if key in _METRICS | _CODES:
        bounds = _METRIC_BOUNDS.get(key)
        return isinstance(value, int) and (
            bounds is None
            or (type(value) is int and bounds[0] <= value <= bounds[1])
        )
    if not isinstance(value, str):
        return False
    if key == "phase":
        return value in _PHASES
    if key == "status":
        return value in {"started", "failed", "passed"}
    if key in {"error_type", "cause_type"}:
        return value.isidentifier()
    return key == "error_module" and bool(
        re.fullmatch(
            r"(?:muninn|install|tests)/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py",
            value,
        )
    )


def _publish(artifact: str, values: dict[str, str | int]) -> None:
    """Write validated fields beneath an explicit private CI scratch leaf."""
    directory = os.environ.get("MUNINN_NATIVE_SCRATCH") or os.environ.get(
        "RUNNER_TEMP"
    )
    if not directory:
        return
    leaf = Path(directory) / "muninn-native-diagnostics"
    ensure_private_dir(leaf)
    path = leaf / f"muninn-{artifact}-proof.json"
    fd = open_private(path, os.O_WRONLY | os.O_CREAT)
    with os.fdopen(fd, "wb") as output:
        output.truncate(0)
        output.write(json.dumps(values, separators=(",", ":")).encode())
        output.flush()
        sync_fd(output.fileno())


def transfer(artifact: str, directory: Path) -> None:
    """Export only validated child phase, code, count and source fields.

    Args:
        artifact: Provider, lifecycle or isolated ordinary-account proof.
        directory: Child's explicit CI scratch directory.
    """
    if artifact not in _ARTIFACTS:
        raise ValueError("unsupported native diagnostic artifact")
    path = (
        directory
        / "muninn-native-diagnostics"
        / f"muninn-{artifact}-proof.json"
    )
    values = _previous(path)
    if values:
        _publish(artifact, values)


def _error_fields(error: Exception) -> dict[str, str | int]:
    """Describe one failure through codes and its last known repo frame."""
    values: dict[str, str | int] = {"error_type": type(error).__name__}
    for current, prefix in ((error, ""), (error.__cause__, "cause_")):
        if current is None:
            continue
        if prefix:
            values["cause_type"] = type(current).__name__
        values.update(
            {
                key: value
                for key in _SECURITY_CODES
                if isinstance(value := getattr(current, key, None), int)
            }
        )
        code = getattr(current, "winerror", None)
        if isinstance(code, int):
            values[prefix + "winerror"] = code
        if (
            not prefix
            and isinstance(current, OSError)
            and current.errno is not None
        ):
            values["errno"] = current.errno
        trace = current.__traceback__
        while trace is not None:
            try:
                module = (
                    Path(trace.tb_frame.f_code.co_filename)
                    .relative_to(_ROOT)
                    .as_posix()
                )
            except ValueError:
                module = ""
            if re.fullmatch(
                r"(?:muninn|install|tests)/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py",
                module,
            ):
                values["error_module"] = module
                values["error_line"] = trace.tb_lineno
            trace = trace.tb_next
    return values


def write(
    artifact: str,
    phase: str | None,
    *,
    error: Exception | None = None,
    metrics: Mapping[str, int] | None = None,
    completed: bool = False,
) -> None:
    """Persist only fixed diagnostic fields in the explicit CI scratch dir.

    Args:
        artifact: Provider, lifecycle or isolated ordinary-account proof.
        phase: Fixed phase code; None retains the previous actual stage.
        error: Failure whose message, locals and arbitrary paths stay private.
        metrics: Explicit numeric route, timing, status or hook counts.
        completed: Mark the proof complete rather than merely started.

    Raises:
        ValueError: If a caller supplies an unsupported phase or metric.
    """
    if artifact not in _ARTIFACTS or (
        phase is not None and phase not in _PHASES
    ):
        raise ValueError("unsupported native diagnostic phase")
    if metrics is not None and (
        not set(metrics) <= _METRICS
        or not all(_safe_field(key, value) for key, value in metrics.items())
    ):
        raise ValueError("unsupported native diagnostic metric")
    directory = os.environ.get("MUNINN_NATIVE_SCRATCH") or os.environ.get(
        "RUNNER_TEMP"
    )
    if not directory:
        return
    path = (
        Path(directory)
        / "muninn-native-diagnostics"
        / f"muninn-{artifact}-proof.json"
    )
    values = _previous(path) if phase is None else {}
    if phase is None and values.get("status") == "failed":
        return
    values["phase"] = phase or str(values.get("phase", "initial"))
    values["status"] = (
        "failed" if error else "passed" if completed else "started"
    )
    values.update(metrics or {})
    if error is not None:
        values.update(_error_fields(error))
    _publish(artifact, values)
