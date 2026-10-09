"""Bound numeric diagnostics and accumulate inclusive lifecycle timings."""

from __future__ import annotations

import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager

TIMING_PHASES = frozenset(
    {
        "fresh_install",
        "fresh_stop",
        "upgrade_install",
        "upgrade_stop",
        "exercise_setup",
        "backend_cleanup",
        "refused_cli",
        "child_setup",
        "outer_setup",
        "outer_task_create",
        "outer_task_run",
        "outer_task_wait",
        "outer_cleanup",
        "cleanup",
    }
)
TIMING_METRICS = frozenset(phase + "_ms" for phase in TIMING_PHASES)
METRIC_BOUNDS = {
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
    "doctor_exit_status": (-1, 65535),
    "doctor_error_length": (0, 65535),
    "doctor_error_words": (0, 4095),
    "task_last_result": (-2147483648, 2147483647),
    "task_state": (0, 4),
}
METRIC_BOUNDS.update(dict.fromkeys(TIMING_METRICS, (0, 4_500_000)))


def timing_fields(values: Mapping[str, str | int]) -> dict[str, str | int]:
    """Retain only validated lifecycle durations across phase transitions."""
    return {
        key: value for key, value in values.items() if key in TIMING_METRICS
    }


@contextmanager
def measure(phase: str, metrics: dict[str, int]) -> Generator[None]:
    """Accumulate one fixed phase's inclusive elapsed time even on failure."""
    if phase not in TIMING_PHASES:
        raise ValueError("unsupported lifecycle timing phase")
    started = time.monotonic()
    try:
        yield
    finally:
        key = phase + "_ms"
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        metrics[key] = min(4_500_000, metrics.get(key, 0) + elapsed)
