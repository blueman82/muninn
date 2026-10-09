"""Private installer diagnostics containing fixed codes and numeric status."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from muninn import platform_io
from muninn.obs_log import clean_record


class Runner(Protocol):
    """A command runner: real subprocesses, or a fake in tests."""

    def __call__(
        self,
        argv: Sequence[str | Path],
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run ``argv`` and return the finished process."""
        ...


def run_real(
    argv: Sequence[str | Path],
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command for real, capturing output.

    Args:
        argv: Program and arguments; paths are converted to strings.
        env: Full environment, or None to inherit.
        input: Bytes for stdin.

    Returns:
        The finished process; the caller inspects the exit status.
    """
    text_argv = [str(a) for a in argv]
    # The timeout is a backstop against a hung tool, not a tuned deadline.
    return subprocess.run(
        text_argv, env=env, input=input, capture_output=True, timeout=600
    )


def _existing(fd: int) -> None:
    """Refuse unsafe opened originals without changing their permissions."""
    platform_io.assert_private_fd(fd)
    if sys.platform != "win32":
        status = os.fstat(fd)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_mode & 0o077
        ):
            raise OSError("installer log ownership or permissions are unsafe")


def _parent(path: Path) -> None:
    """Validate the existing log parent without modifying access control."""
    if sys.platform != "win32" and path.parent.exists():
        status = path.parent.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_mode & 0o077
        ):
            raise OSError("installer log directory is unsafe")
    elif path.parent.exists() and not platform_io.is_private(
        path.parent, directory=True
    ):
        raise OSError("installer log directory is unsafe")


def _open(path: Path) -> int:
    """Open a validated private append descriptor or create it exclusively."""
    _parent(path)
    platform_io.ensure_private_dir(path.parent)
    try:
        return platform_io.open_private(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
        )
    except FileExistsError:
        flags = os.O_WRONLY | os.O_APPEND
        if sys.platform == "win32":
            fd = platform_io.open_private(path, flags)
        else:
            fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            _existing(fd)
            return fd
        except BaseException:
            os.close(fd)
            raise


def install_log(
    path: Path, run: Runner = run_real
) -> tuple[Callable[[str], None], Runner]:
    """Wrap post-preflight progress and commands with private code-only logs.

    Args:
        path: Private installer log location.
        run: Existing command runner to wrap.

    Returns:
        The progress printer and wrapped command runner.

    Raises:
        OSError: If an existing log or its directory is unsafe.
    """
    _parent(path)
    if path.exists() or path.is_symlink():
        fd = _open(path)
        os.close(fd)

    def note(code: str, status: int | None = None) -> None:
        entry: dict[str, object] = {"event": code, "at": round(time.time(), 3)}
        if status is not None:
            entry["exit"] = status
        with os.fdopen(_open(path), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(clean_record(entry)) + "\n")

    def say(text: str) -> None:
        print(text)
        note("install_progress")

    def logged(
        argv: Sequence[str | Path],
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        result = run(argv, env=env, input=input)
        note("install_command", result.returncode)
        return result

    return say, logged
