"""Load explicit launch selections and digest-bound trusted instructions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from muninn.knowledge_models import SchemaVersion, StrictModel, Text
from muninn.review_json import finite_float, reject_constant, unique_object
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_runner import (
    RunnerCapability,
    check_private_path,
    read_private_file,
)
from muninn.store import data_home


class PromptArtifact(StrictModel):
    """Bind trusted instructions to one explicit internal assembly format."""

    schema_version: SchemaVersion
    assembly: Literal["muninn-review-prompt-v1"]
    instructions: Text


def _load(home: Path, path: Path, limit: int) -> bytes:
    """Check the configured private directory chain and exact bounded file."""
    try:
        if os.name != "posix":
            raise ValueError("launch_input_platform")
        if home != data_home(os.environ) or home not in path.parents:
            raise ValueError("launch_input_storage")
        check_private_path(home, directory=True)
        for parent in reversed(path.parents):
            if home in parent.parents:
                check_private_path(parent, directory=True)
        check_private_path(path, directory=False)
        raw = read_private_file(path, limit)
    except OSError:
        raise ValueError("launch_input_unavailable") from None
    try:
        json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (ValueError, RecursionError):
        raise ValueError("launch_input_invalid") from None
    return raw


def load_capability(home: Path, policy: ReviewPolicy) -> RunnerCapability:
    """Read exactly the complete policy's explicit capability selection."""
    try:
        policy = ReviewPolicy.model_validate_json(policy.model_dump_json())
        raw = _load(
            home,
            home
            / "review-capabilities"
            / "selected"
            / (digest(policy) + ".json"),
            1024 * 1024,
        )
        capability = RunnerCapability.model_validate_json(raw)
        if any(
            getattr(capability, key) != value
            for key, value in policy.runner.model_dump().items()
        ):
            raise ValueError("launch_input_binding")
        return capability
    except ValidationError:
        raise ValueError("launch_input_invalid") from None


def load_prompt(home: Path, policy: ReviewPolicy) -> bytes:
    """Return validated exact artifact bytes without publishing a prompt."""
    try:
        policy = ReviewPolicy.model_validate_json(policy.model_dump_json())
        raw = _load(
            home,
            home / "review-prompts" / (policy.prompt_digest + ".json"),
            policy.max_input_bytes,
        )
        if hashlib.sha256(raw).hexdigest() != policy.prompt_digest:
            raise ValueError("launch_input_binding")
        PromptArtifact.model_validate_json(raw)
        return raw
    except ValidationError:
        raise ValueError("launch_input_invalid") from None
