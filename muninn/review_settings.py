"""Read and write the learner settings file and derive the fixed policy."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from muninn import file_sync, platform_io
from muninn.knowledge_models import StrictModel, Text
from muninn.review_instructions import PROMPT_DIGEST
from muninn.review_json import canonical_json_value, result_schema
from muninn.review_models import PilotBudget, ReviewPolicy, RunnerConfig

FILE_NAME = "learning.json"


class Settings(StrictModel):
    """Choose whether learning runs and which model call runs it."""

    enabled: bool = False
    provider: Literal["claude", "codex"] = "claude"
    model: Text = "sonnet"
    effort: Text = "low"


def load(home: Path) -> Settings:
    """Read the settings file; a missing file gives the defaults.

    Args:
        home: Muninn data directory.

    Returns:
        The validated settings.

    Raises:
        ValueError: If the file exists but is not valid settings.
    """
    try:
        raw = (home / FILE_NAME).read_bytes()
    except FileNotFoundError:
        return Settings()
    except OSError:
        raise ValueError("learning_settings_unavailable") from None
    try:
        return Settings.model_validate_json(raw)
    except ValidationError:
        raise ValueError("learning_settings_invalid") from None


def save(home: Path, settings: Settings) -> None:
    """Replace the settings file atomically with owner-only access.

    Args:
        home: Muninn data directory.
        settings: Settings to store.
    """
    platform_io.ensure_private_dir(home)
    fd, name = tempfile.mkstemp(dir=home, prefix=".learning-")
    with os.fdopen(fd, "wb") as handle:
        handle.write(settings.model_dump_json().encode())
        handle.flush()
        file_sync.sync_fd(handle.fileno())
    Path(name).replace(home / FILE_NAME)
    file_sync.sync_path(home)


def policy_for(settings: Settings) -> ReviewPolicy:
    """Build the constant review policy for the chosen model and effort.

    Args:
        settings: Learner settings.

    Returns:
        The policy; only the runner depends on the settings.
    """
    codex = settings.provider == "codex"
    return ReviewPolicy(
        schema_version=1,
        policy_id="muninn-learner",
        prompt_digest=PROMPT_DIGEST,
        projection_version="permitted-fields-v2",
        result_schema_digest=hashlib.sha256(
            canonical_json_value(result_schema()).encode()
        ).hexdigest(),
        runner=RunnerConfig(
            runner="codex_cli" if codex else "claude_cli",
            executable="codex" if codex else "claude",
            version="unversioned",
            model=settings.model,
            reasoning=settings.effort,
        ),
        max_rounds=2,
        max_input_bytes=1_048_576,
        max_output_bytes=32768,
        context_bytes=12000,
        deadline_seconds=120.0,
        max_attempts=3,
        retry_backoff_seconds=[5.0, 30.0],
        concurrency=1,
        hook_wait_seconds=0.0,
        allow_global=False,
        # Throwaway until the budget field is removed from ReviewPolicy.
        budget=PilotBudget(
            account_id="learner",
            max_model_calls=1_000_000,
            max_wall_seconds=1e9,
            max_cost_decimal=None,
            conservative_call_cost_decimal=None,
            currency=None,
            approved_by="muninn",
            approved_at=0.0,
        ),
    )
