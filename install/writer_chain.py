"""Bind Windows writer processes to one selected venv hop and task parent."""

from __future__ import annotations

from pathlib import Path

from install.context import Ctx, StepFailedError, must
from muninn.obs_service import parse_process, process_command, same_path


def _positive_pid(record: dict[str, object], field: str) -> int:
    """Require a positive observed process id or parent id."""
    value = record.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise StepFailedError("writer chain identity is unavailable")
    return value


def _created(record: dict[str, object]) -> str:
    """Require a nonempty process creation identity."""
    value = record.get("created")
    if not isinstance(value, str) or not value:
        raise StepFailedError("writer chain creation is unavailable")
    return value


def _inspect(ctx: Ctx, pid: int) -> dict[str, object]:
    """Inspect exactly one numeric process and bind its returned identity."""
    process = parse_process(must(ctx, process_command(pid), quiet=True).stdout)
    if _positive_pid(process, "pid") != pid:
        raise StepFailedError("writer chain process changed")
    _positive_pid(process, "parent")
    _created(process)
    return process


def windows_chain(
    ctx: Ctx,
    writer: dict[str, object],
    selected: tuple[Path, Path] | None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Inspect the task parent through at most one selected venv redirector."""
    _positive_pid(writer, "pid")
    _created(writer)
    immediate = _inspect(ctx, _positive_pid(writer, "parent"))
    executable = immediate.get("exe")
    if (
        selected is not None
        and isinstance(executable, str)
        and same_path(executable, selected[1])
    ):
        return _inspect(ctx, _positive_pid(immediate, "parent")), immediate
    return immediate, None


def same_process(ctx: Ctx, process: dict[str, object]) -> bool:
    """Compare a process's current PID and creation with its observation."""
    pid = _positive_pid(process, "pid")
    created = _created(process)
    result = ctx.run(process_command(pid))
    if result.returncode == 3:
        return False
    if result.returncode:
        raise StepFailedError("cannot establish process quiescence")
    current = parse_process(result.stdout)
    return (
        _positive_pid(current, "pid") == pid and _created(current) == created
    )
