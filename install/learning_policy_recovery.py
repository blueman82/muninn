"""Validate explicit recovery of a known ongoing learning policy."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from install.context import StepFailedError
from muninn.review_models import OngoingBudget, ReviewPolicy
from tools import prepare_codex_policy

_REVISIONS = {
    "muninn-codex-ongoing-production": 1,
    "muninn-codex-ongoing-production-v2": 2,
    "muninn-codex-ongoing-production-v3": 3,
}


def bundled(policy: ReviewPolicy) -> bool:
    """Recognise an exact legacy profile with its recorded CLI version."""
    revision = _REVISIONS.get(policy.policy_id)
    if (
        revision is None
        or policy.experimental_codex is None
        or not isinstance(policy.budget, OngoingBudget)
    ):
        return False
    expected, _ = prepare_codex_policy.policy(
        policy.experimental_codex.data_home,
        Path(policy.runner.executable),
        policy.budget.approved_by,
        policy.budget.approved_at,
        revision=revision,
        repository_default=policy.policy_source == "repository_default",
    )
    expected = expected.model_copy(
        update={
            "runner": expected.runner.model_copy(
                update={"version": policy.runner.version}
            )
        }
    )
    return policy == expected


def eligible(policy: ReviewPolicy) -> bool:
    """Limit adoption to defaults and exact legacy profiles."""
    return policy.policy_source == "repository_default" or bundled(policy)


def require_finished(conn: sqlite3.Connection) -> None:
    """Refuse explicit restart while any recorded call remains uncertain."""
    unsafe = conn.execute(
        "SELECT 1 FROM review_attempt WHERE ended_at IS NULL "
        "UNION ALL SELECT 1 FROM review_call WHERE state!='completed' "
        "UNION ALL SELECT 1 FROM review_request WHERE state='running' LIMIT 1"
    ).fetchone()
    if unsafe is not None:
        raise StepFailedError(
            "learning_recovery_unverified: an earlier review may "
            "still be active"
        )


def profile(
    raw: bytes, installed: ReviewPolicy | None, enable: bool, now: float
) -> bytes:
    """Freeze renewed approval with the existing runner and bounds."""
    if installed is None:
        return raw
    value = cast(dict[str, Any], json.loads(raw))
    if value.get("default_revision") != 3:
        if enable:
            raise StepFailedError("learning_recovery_policy_required")
        return raw
    value["runner"]["version"] = installed.runner.version
    for name in (
        "max_rounds",
        "max_output_bytes",
        "context_bytes",
        "deadline_seconds",
    ):
        value["limits"][name] = (
            getattr(installed, name)
            if installed.policy_id.endswith("-v3") and enable
            else min(value["limits"][name], getattr(installed, name))
        )
    if installed.policy_id != "muninn-codex-ongoing-production":
        value["revisions"]["3"]["max_input_bytes"] = (
            installed.max_input_bytes
            if installed.policy_id.endswith("-v3") and enable
            else min(
                value["revisions"]["3"]["max_input_bytes"],
                installed.max_input_bytes,
            )
        )
    if installed.policy_id.endswith("-v3"):
        value["revisions"]["3"].update(
            concurrency=installed.concurrency,
            max_attempts=installed.max_attempts,
            retry_backoff_seconds=installed.retry_backoff_seconds,
        )
    if enable:
        if (
            not isinstance(installed.budget, OngoingBudget)
            or now <= installed.budget.approved_at
        ):
            raise StepFailedError(
                "learning_recovery_clock: approval time must "
                "follow the installed policy"
            )
        value["approved_at"] = datetime.fromtimestamp(now, UTC).isoformat()
        value["approved_by"] = "Local installer approval"
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
