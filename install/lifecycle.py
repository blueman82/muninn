"""Per-user launchd service with graceful, verified writer pause."""

from __future__ import annotations

from install.context import Ctx, job, wait
from muninn.platform_lock import released


def stop(ctx: Ctx) -> None:
    """Unload the owned service and wait for the writer to finish."""
    ctx.run(["launchctl", "bootout", ctx.target])
    wait(ctx, lambda: not job(ctx) and released(ctx.data), 30, "busy")
