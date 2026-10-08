"""World-owned patch registration and a non-atomic Windows link model."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


def relink(home: Path, link: Path, target: str | Path, ts: str) -> None:
    """Model link outcomes only inside the fixture's exact link namespace.

    This deliberately does not model atomic visibility. POSIX hosts use
    the production primitive; native Windows tests use selection.json.

    Args:
        home: The fixture's owned home directory.
        link: One of its current, interpreter or runtime links.
        target: The symlink target, whose contents are never changed.
        ts: The synthetic run identifier used for the temp name.
    """
    root = home.resolve(strict=True)
    allowed = {
        home / ".local/lib/muninn/current",
        home / ".local/lib/muninn/python",
        home / ".local/bin/muninn",
    }
    if home.is_symlink() or link not in allowed:
        raise ValueError("fixture_link_outside_home")
    if not link.parent.resolve().is_relative_to(root):
        raise ValueError("fixture_link_parent_escape")
    if link.exists() and not link.is_symlink():
        raise ValueError("fixture_link_not_symlink")
    pending = link.with_name(f".{link.name}.{ts}")
    if pending.exists() or pending.is_symlink():
        raise FileExistsError("fixture_link_temp_exists")
    link.parent.mkdir(parents=True, exist_ok=True)
    pending.symlink_to(target)
    if link.exists() and not link.is_symlink():
        raise ValueError("fixture_link_not_symlink")
    if link.is_symlink():
        link.unlink()
    pending.rename(link)


def register(tc: unittest.TestCase, home: Path) -> None:
    """Register fake installer boundaries for one owned World.

    Args:
        tc: Test case that owns patch cleanup.
        home: The World home, assigned before registration.
    """
    identity = mock.patch(
        "install.steps_config.codex_identity",
        return_value=("synthetic-codex-candidate",),
    )
    identity.start()
    tc.addCleanup(identity.stop)
    if sys.platform != "win32":
        return

    def owned_relink(link: Path, target: str | Path, ts: str) -> None:
        relink(home, link, target, ts)

    for name in ("install.steps_release.relink", "install.rollback.relink"):
        boundary = mock.patch(name, owned_relink)
        boundary.start()
        tc.addCleanup(boundary.stop)
