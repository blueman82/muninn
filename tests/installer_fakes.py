"""World-owned patch registration and a non-atomic Windows link model."""

from __future__ import annotations

import json
import shlex
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from install.context import Ctx
from install.provider_paths import render_pinned
from install.uninstall import _is_ours as original_is_ours


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


def render_hooks(home: Path, ctx: Ctx, relative: str, data: bytes) -> bytes:
    """Model POSIX command spelling only for this World's Darwin hooks.

    Args:
        home: Owned fixture home.
        ctx: Actual rendering context.
        relative: Pinned template name.
        data: Original template bytes.

    Returns:
        Original rendering except the exact owned shell command prefix.
    """
    rendered = render_pinned(ctx, relative, data)
    if (
        ctx.platform != "darwin"
        or ctx.home != home
        or ctx.muninn != home / ".local/bin/muninn"
        or relative
        not in (
            "integrations/claude/settings-hooks.json",
            "integrations/codex/hooks/hooks.json",
        )
    ):
        return rendered
    prefix = shlex.quote(str(ctx.muninn)) + " hook "
    replacement = shlex.quote(ctx.muninn.as_posix()) + " hook "
    if prefix == replacement:
        return rendered
    obj: dict[str, Any] = json.loads(rendered)
    changed = False
    for groups in obj["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                command = handler.get("command", "")
                if command.startswith(prefix):
                    handler["command"] = replacement + command[len(prefix) :]
                    changed = True
    return (
        (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode()
        if changed
        else rendered
    )


def owns_link(home: Path, ctx: Ctx) -> bool:
    """Model Darwin link ownership through actual confined native targets.

    Args:
        home: Owned fixture home.
        ctx: Uninstall context.

    Returns:
        Whether the exact fixture link resolves inside its owned library.
    """
    if ctx.platform != "darwin" or ctx.home != home:
        return original_is_ours(ctx)
    if (
        home.is_symlink()
        or ctx.muninn != home / ".local/bin/muninn"
        or ctx.lib != home / ".local/lib/muninn"
        or not ctx.muninn.is_symlink()
    ):
        return False
    root = home.resolve(strict=True)
    library = ctx.lib.resolve(strict=False)
    if not ctx.muninn.parent.resolve().is_relative_to(root):
        return False
    if not library.is_relative_to(root):
        return False
    return ctx.muninn.resolve(strict=False).is_relative_to(library)


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

    def owned_render(ctx: Ctx, relative: str, data: bytes) -> bytes:
        return render_hooks(home, ctx, relative, data)

    def owned_link(ctx: Ctx) -> bool:
        return owns_link(home, ctx)

    for name in (
        "install.preflight.render_pinned",
        "install.steps_release.render_pinned",
    ):
        rendering = mock.patch(name, owned_render)
        rendering.start()
        tc.addCleanup(rendering.stop)
    ownership = mock.patch("install.uninstall._is_ours", owned_link)
    ownership.start()
    tc.addCleanup(ownership.stop)
    for name in ("install.steps_release.relink", "install.rollback.relink"):
        boundary = mock.patch(name, owned_relink)
        boundary.start()
        tc.addCleanup(boundary.stop)
