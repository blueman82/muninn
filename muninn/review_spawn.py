"""Create and transfer ownership of the fixed idle review wrapper."""

from __future__ import annotations

import os
import subprocess

from muninn.review_launch_spec import LaunchSpec
from muninn.review_models import ProcessIdentity
from muninn.review_process import (
    ProcessMonitor,
    RunningProcess,
    adopt_after_registration_error,
)
from muninn.review_process_identity import identify


def create_wrapper(
    spec: LaunchSpec,
    monitor: ProcessMonitor,
    attempt_id: str,
    round_index: int,
    output_limit: int,
) -> RunningProcess:
    """Contain a concrete owned child even when registration fails."""
    proc = subprocess.Popen(
        spec.wrapper_argv,
        cwd=spec.wrapper_cwd,
        env=spec.wrapper_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name != "nt",
    )
    try:
        identity = identify(proc.pid)
    except OSError:
        identity = ProcessIdentity(
            pid=proc.pid,
            created="unverified-owned-child",
            pgid=proc.pid if os.name != "nt" else None,
            job_marker=None,
        )
    handle = RunningProcess(
        attempt_id, round_index, proc, identity, spec.deadline, output_limit
    )
    try:
        monitor.register(handle)
    except Exception:
        adopt_after_registration_error(monitor, handle)
    return handle
