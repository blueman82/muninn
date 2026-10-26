"""Verify runtime and managed configuration before policy activation."""

from __future__ import annotations

import os
from pathlib import Path

from install import legacy_baseline as baseline
from install import policy_records
from install.context import Ctx, StepFailedError, link_text
from install.policy_record_models import Selection
from install.record import Record


def selection(ctx: Ctx) -> Selection:
    """Bind all selected launch paths and the configured plugin cache."""
    links: dict[str, list[str | int]] = {}
    for path in (ctx.lib / "current", ctx.lib / "python", ctx.muninn):
        if not path.is_symlink():
            raise StepFailedError("review_activation_runtime_changed")
        info = path.lstat()
        links[str(path)] = [link_text(path), info.st_dev, info.st_ino]
    return policy_records.selection(
        {
            "links": links,
            "cache": (
                baseline.tree_state(ctx.cache, private=False)
                if os.path.lexists(ctx.cache)
                else None
            ),
        }
    )


def verify_ready(ctx: Ctx, rec: Record) -> None:
    """Require the exact completed compatible configuration during recovery."""
    wanted = policy_records.ready(rec.get("policy_ready"), ctx)
    if (
        not wanted
        or baseline.tree_state(ctx.lib / rec["sha"], links=True)
        != wanted["runtime"]
    ):
        raise StepFailedError("review_activation_runtime_changed")
    for name, expected in wanted["files"].items():
        path = Path(name)
        if (
            baseline.file_state(path) if os.path.lexists(path) else None
        ) != expected:
            raise StepFailedError("review_activation_configuration_changed")
    actual = selection(ctx)
    if actual != wanted["selection"]:
        raise StepFailedError("review_activation_runtime_changed")
