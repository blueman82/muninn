"""Inspect unreaped Unix group leaders without runtime validation packages."""

from __future__ import annotations

import ctypes
import os
import sys


def sole_zombie(pid: int) -> bool:
    """Prove a Darwin group contains only our unreaped exited child."""
    if sys.platform != "darwin":
        return False
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
