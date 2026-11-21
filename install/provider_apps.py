"""Detect installed provider applications independently of saved history."""

from __future__ import annotations

import shutil
from pathlib import Path

from install.context import Ctx

APPLICATIONS = Path("/Applications")
_APP_NAMES = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor"}


def installed(ctx: Ctx, provider: str) -> bool:
    """Check native apps, including a standalone Codex CLI on macOS.

    Args:
        ctx: Installation home and native platform.
        provider: Claude, Codex or Cursor's lowercase provider name.

    Returns:
        Whether the app bundle or provider executable is installed.
    """
    name = f"{_APP_NAMES[provider]}.app"
    return any(
        (root / name).is_dir()
        for root in (APPLICATIONS, ctx.home / "Applications")
    ) or (provider == "codex" and shutil.which("codex") is not None)
