"""Observe terminal POSIX group membership without releasing its leader."""

from __future__ import annotations

import errno
import os
import struct
import sys
import time
from pathlib import Path

from muninn.review_process_posix import sole_zombie


def _read(path: Path, limit: int) -> bytes:
    """Read one fresh bounded native observation."""
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("native_observation_limit")
    return data


def _parse(data: bytes, pid: int) -> tuple[int, int, str, int, int]:
    """Extract group, state, thread count and birth ticks."""
    prefix, separator, tail = data.rpartition(b") ")
    fields = tail.split()
    identity, opening, _name = prefix.partition(b" (")
    if not separator or not opening or identity != str(pid).encode():
        raise ValueError("native_process_identity")
    if len(fields) < 20:
        raise ValueError("native_process_fields")
    group, sid = int(fields[2]), int(fields[3])
    threads, birth = int(fields[17]), int(fields[19])
    state = fields[0].decode("ascii")
    if group < 0 or sid < 0 or threads < 1 or birth < 0 or len(state) != 1:
        raise ValueError("native_process_fields")
    return group, sid, state, threads, birth


def _mount_visible(mountinfo: str) -> bool:
    """Reject hidden processes and overlays on native observations."""
    roots = 0
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 6 or len(filesystem) != 3:
            return False
        target = fields[4]
        if target == "/proc":
            roots += 1
            if fields[3] != "/" or filesystem[0] != "proc":
                return False
            options = fields[5].split(",") + filesystem[2].split(",")
            if any(
                option.startswith("hidepid=") and option != "hidepid=0"
                for option in options
            ):
                return False
        elif target.startswith("/proc/"):
            first = target.removeprefix("/proc/").split("/", 1)[0]
            if first.isdecimal() or first in {"self", "thread-self", "stat"}:
                return False
    return roots == 1


def _visible() -> bool:
    """Require unmasked procfs in our current PID namespace."""
    if os.name == "nt":
        return False
    mounts = os.fsdecode(_read(Path("/proc/self/mountinfo"), 1048576))
    if not _mount_visible(mounts) or str(Path("/proc/self").readlink()) != str(
        os.getpid()
    ):
        return False
    status = _read(Path("/proc/self/status"), 65536)
    namespace = [
        line.split()[1:]
        for line in status.splitlines()
        if line.startswith(b"NSpid:")
    ]
    if namespace != [[str(os.getpid()).encode()]]:
        return False
    own = _parse(_read(Path("/proc/self/stat"), 4096), os.getpid())
    return own[0] == os.getpgrp() and own[1] == os.getsid(0)


def _counter() -> tuple[int, int]:
    """Bind a fresh global fork counter to its native boot timestamp."""
    values: dict[bytes, int] = {}
    for line in _read(Path("/proc/stat"), 1048576).splitlines():
        fields = line.split()
        if fields and fields[0] in {b"btime", b"processes"}:
            if len(fields) != 2 or fields[0] in values:
                raise ValueError("native_fork_counter")
            values[fields[0]] = int(fields[1])
    boot, forks = values[b"btime"], values[b"processes"]
    if boot < 0 or not 0 <= forks < 2**64 - 1:
        raise ValueError("native_fork_counter")
    return boot, forks


def _members(pgid: int, deadline: float) -> dict[int, tuple[str, int, int]]:
    """Enumerate a complete bounded group view, refusing unreadable entries."""
    found: dict[int, tuple[str, int, int]] = {}
    with os.scandir("/proc") as entries:
        for entry in entries:
            if time.monotonic() >= deadline:
                raise TimeoutError("native_observation_deadline")
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                group, sid, state, threads, birth = _parse(
                    _read(Path(entry.path) / "stat", 4096), pid
                )
            except OSError as error:
                if error.errno in {errno.ENOENT, errno.ESRCH}:
                    continue
                raise
            if pid == pgid and (group != pgid or sid != pgid):
                raise ValueError("native_session_identity")
            if sid == pgid:
                found[pid] = state, threads, birth
    return found


def _leader_terminal(pgid: int) -> bool:
    """Observe our exited child without reaping, ordering fork publications."""
    if os.name == "nt":
        raise OSError("native_observation_unsupported")
    result = os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    return (
        result is not None
        and result.si_pid == pgid
        and result.si_code in {os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED}
    )


def _linux_quiescent(pgid: int) -> bool:
    """Bracket terminal session membership with publication barriers."""
    deadline = time.monotonic() + 0.1
    if not _visible():
        return False
    before = _counter()
    if not _leader_terminal(pgid):
        return False
    first = _members(pgid, deadline)
    second = _members(pgid, deadline)
    if not _leader_terminal(pgid):
        return False
    after = _counter()
    # WNOWAIT takes the native task-list read lock without releasing our
    # leader. Counter reads bracket those barriers and terminal observations;
    # an unchanged count excludes children missed during enumeration.
    return (
        time.monotonic() < deadline
        and before == after
        and pgid in first
        and first == second
        and all(
            state in {"Z", "X"} and threads == 1
            for state, threads, _ in first.values()
        )
    )


def group_quiescent(pgid: int) -> bool:
    """Observe platform-specific shutdown without consuming the owned leader.

    Args:
        pgid: Reserved PID and PGID of the exclusively owned unreaped leader.

    Returns:
        Linux requires terminal whole-session membership under a fork-counter
        bracket. Darwin requires only the native sole-current-group zombie;
        same-session escape/rejoin and closed-session safety are unverified.
        Unknown evidence refuses. This observer never signals or reaps.
    """
    try:
        if pgid > 0 and sys.platform == "darwin":
            return sole_zombie(pgid)
        if pgid <= 0 or sys.platform != "linux" or struct.calcsize("L") != 8:
            return False
        return _linux_quiescent(pgid)
    except (OSError, ValueError, KeyError):
        return False
