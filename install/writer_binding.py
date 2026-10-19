"""Bind a running Windows writer and its launcher to the owned task."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from muninn.obs_service import (
    same_path,
    writer_command_matches,
    writer_image_matches,
)


def writer_mismatch(
    selected: tuple[Path, Path] | None,
    writer: dict[str, object],
    launcher: dict[str, object],
    writer_command: str,
    task: tuple[str | None, str | None],
    engines: Collection[int],
    *,
    redirector: dict[str, object] | None = None,
) -> int | None:
    """Find the first way the live processes differ from the owned task.

    Args:
        selected: The pinned release and interpreter, or None when unknown.
        writer: The writer process facts.
        launcher: The launcher process facts.
        writer_command: The writer command line the scheduler reported.
        task: The owned task's command and arguments.
        engines: Process ids of the task's running instances.
        redirector: The one selected venv hop, when the base image runs.

    Returns:
        The 1-based number of the first failing check, or None when both
        processes are the owned task's.
    """
    command, args = task
    executable, launcher_exe = writer["exe"], launcher["exe"]
    launcher_command = launcher["cmd"]
    checks = (
        selected is not None,
        isinstance(executable, str),
        selected is not None
        and isinstance(executable, str)
        and writer_image_matches(selected, executable),
        selected is not None
        and writer_command_matches(selected, writer_command),
        isinstance(launcher_exe, str) and bool(command),
        isinstance(launcher_exe, str)
        and bool(command)
        and same_path(launcher_exe, command),
        isinstance(launcher_command, str) and bool(args),
        isinstance(launcher_command, str)
        and bool(args)
        and str(args) in launcher_command,
        bool({launcher["pid"], launcher["parent"]} & set(engines)),
        selected is not None
        and _hop_matches(selected, writer, launcher, redirector),
    )
    for number, ok in enumerate(checks, start=1):
        if not ok:
            return number
    return None


def _hop_matches(
    selected: tuple[Path, Path],
    writer: dict[str, object],
    launcher: dict[str, object],
    redirector: dict[str, object] | None,
) -> bool:
    """Require either a direct parent or exactly one selected venv hop."""
    writer_exe, writer_parent = writer["exe"], writer["parent"]
    launcher_pid = launcher["pid"]
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (writer_parent, launcher_pid)
    ):
        return False
    direct = isinstance(writer_exe, str) and same_path(writer_exe, selected[1])
    if redirector is None:
        return direct and writer_parent == launcher_pid
    intermediate_pid = redirector.get("pid")
    intermediate_parent = redirector.get("parent")
    intermediate_exe = redirector.get("exe")
    intermediate_cmd = redirector.get("cmd")
    return (
        not direct
        and isinstance(intermediate_pid, int)
        and not isinstance(intermediate_pid, bool)
        and intermediate_pid > 0
        and isinstance(intermediate_parent, int)
        and not isinstance(intermediate_parent, bool)
        and intermediate_parent > 0
        and writer_parent == intermediate_pid
        and intermediate_parent == launcher_pid
        and isinstance(intermediate_exe, str)
        and same_path(intermediate_exe, selected[1])
        and isinstance(intermediate_cmd, str)
        and writer_command_matches(selected, intermediate_cmd)
    )
