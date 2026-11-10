"""Check private pause-file identities without loading recovery state."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from muninn import platform_io

type Identity = tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(ge=0)]]
type RefusalCode = Literal[
    "coordination_timeout",
    "owner_changed",
    "reboot_required",
    "boot_identity_unavailable",
    "legacy_volume_confirmation_required",
    "pause_unverified",
]


class PauseRefusedError(ValueError):
    """Report a finite coordination refusal without exposing content."""

    def __init__(self, code: RefusalCode) -> None:
        """Keep the original finite reason available to the caller."""
        self.code = code
        super().__init__(code)


class SnapshotChangedError(PauseRefusedError):
    """Refuse discarded evidence after a safe pathname changed identity."""

    def __init__(self) -> None:
        """Preserve the public refusal code without certifying publication."""
        super().__init__("pause_unverified")


def private(path: Path, *, directory: bool = False) -> os.stat_result:
    """Reject links, public modes and foreign ownership before opening."""
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or not platform_io.is_private(
        path, directory=directory
    ):
        raise PauseRefusedError("pause_unverified")
    if os.name != "nt" and info.st_uid != os.getuid():
        raise PauseRefusedError("pause_unverified")
    return info


def identity(info: os.stat_result) -> Identity:
    """Project the native descriptor identity used by the lock protocol."""
    return info.st_dev, info.st_ino


def directory_identity(home: Path) -> Identity:
    """Read one verified private data directory identity."""
    return identity(private(home, directory=True))


def lock_identity(home: Path, fd: int) -> Identity:
    """Require a private unchanged lock path matching the held descriptor."""
    private(home, directory=True)
    path = private(home / "review.supervisor.lock")
    held = os.fstat(fd)
    if not stat.S_ISREG(held.st_mode) or identity(path) != identity(held):
        raise PauseRefusedError("pause_unverified")
    return identity(held)
