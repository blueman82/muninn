"""Inspect disposable process completion with native host primitives."""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    import _winapi


def process_gone(pid: int) -> bool:
    """Require a missing or exited process; propagate unverified state."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("invalid_fixture_pid")
    if sys.platform == "win32":
        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError as error:
            if getattr(error, "winerror", None) == 87:
                return True
            raise
        try:
            return _winapi.WaitForSingleObject(handle, 0) == (
                _winapi.WAIT_OBJECT_0
            )
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False
