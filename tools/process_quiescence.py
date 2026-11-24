"""Observe terminal Darwin group membership without releasing its leader."""

from __future__ import annotations

import ctypes
import os


def sole_zombie(pid: int) -> bool:
    """Prove a Darwin group contains only our unreaped exited child."""
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    query = library.proc_listpids
    query.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    query.restype = ctypes.c_int
    members = (ctypes.c_int * 2)()
    size = query(2, pid, members, ctypes.sizeof(members))
    if size != ctypes.sizeof(ctypes.c_int) or members[0] != pid:
        return False
    exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    return exited is not None and exited.si_pid == pid


def group_quiescent(pgid: int) -> bool:
    """Observe group shutdown without consuming the owned leader.

    Args:
        pgid: Reserved PID and PGID of the exclusively owned unreaped leader.

    Returns:
        True only when the native sole-current-group zombie is proved;
        same-session escape/rejoin and closed-session safety are unverified.
        Unknown evidence refuses. This observer never signals or reaps.
    """
    try:
        return pgid > 0 and sole_zombie(pgid)
    except (OSError, ValueError, KeyError):
        return False
