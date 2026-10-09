"""Bind a running Windows writer and its launcher to the owned task."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from muninn.obs_service import command_names_path, same_path


def writer_mismatch(
    selected: tuple[Path, Path] | None,
    writer: dict[str, object],
    launcher: dict[str, object],
    writer_command: str,
    task: tuple[str | None, str | None],
    engines: Collection[int],
) -> int | None:
    """Find the first way the live processes differ from the owned task.

    Args:
        selected: The pinned release and interpreter, or None when unknown.
        writer: The writer process facts.
        launcher: The launcher process facts.
        writer_command: The writer command line the scheduler reported.
        task: The owned task's command and arguments.
        engines: Process ids of the task's running instances.

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
        and same_path(executable, selected[1]),
        selected is not None
        and command_names_path(writer_command, selected[0]),
        isinstance(launcher_exe, str) and bool(command),
        isinstance(launcher_exe, str)
        and bool(command)
        and same_path(launcher_exe, command),
        isinstance(launcher_command, str) and bool(args),
        isinstance(launcher_command, str)
        and bool(args)
        and str(args) in launcher_command,
        bool({launcher["pid"], launcher["parent"]} & set(engines)),
    )
    for number, ok in enumerate(checks, start=1):
        if not ok:
            return number
    return None
