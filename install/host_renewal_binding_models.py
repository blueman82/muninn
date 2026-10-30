"""Strict approved binding inputs without installer dependencies."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ConfigDict, with_config
from typing_extensions import TypedDict

from install.policy_record_models import (
    Directory,
    FileState,
    Identity,
    Selection,
    TreeState,
)
from muninn.knowledge_models import Digest, StrictModel
from muninn.review_codex_launch import ExperimentalBinding
from muninn.review_json import canonical_json_value


@with_config(ConfigDict(strict=True, extra="forbid"))
class ConfigurationFile(TypedDict):
    """Bind original configuration and approved transform hashes."""

    path: str
    before: FileState | None
    expected: list[Digest]


@with_config(ConfigDict(strict=True, extra="forbid"))
class Configuration(TypedDict):
    """Bind exactly the three supported provider configuration paths."""

    cursor_hooks: bool
    claude: ConfigurationFile | None
    codex: ConfigurationFile | None
    cursor: ConfigurationFile | None


@with_config(ConfigDict(strict=True, extra="forbid"))
class TargetSelection(TypedDict):
    """Retain the expected upgrade link text without inventing identities."""

    current: str
    launcher: str
    python: str


@with_config(ConfigDict(strict=True, extra="forbid"))
class InstallationInputs(TypedDict):
    """Retain every field in the original complete installation capture."""

    home: Directory
    repo: Directory
    library: Directory
    sha: str
    runtime: TreeState
    selection: Selection
    python: FileState
    service: FileState
    configuration: Configuration
    target_contract: Digest
    target_files: dict[str, Digest]
    expected_selection: TargetSelection
    operation_lock: FileState | None


@with_config(ConfigDict(strict=True, extra="forbid"))
class ExecutableInput(TypedDict):
    """Retain the checked executable target, digest and file observation."""

    target: str
    digest: Digest
    file: FileState


@with_config(ConfigDict(strict=True, extra="forbid"))
class ArtifactInputs(TypedDict):
    """Retain exact selected artifact inputs without authentication bodies."""

    artifacts: dict[str, FileState]
    cwd: Directory
    auth: Directory
    executable: ExecutableInput
    binding: ExperimentalBinding
    config_digest: Digest


class BindingInputs(StrictModel):
    """Retain the complete approved preimage for exact phase validation."""

    installation: InstallationInputs
    artifacts: ArtifactInputs
    protected_files: tuple[
        FileState | None, FileState | None, FileState | None, Identity
    ]

    def digest(self) -> str:
        """Hash the unchanged original canonical binding-input recipe."""
        return hashlib.sha256(
            canonical_json_value(self.model_dump(mode="json")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class HeldOperation:
    """Retain observations within the lifetime of the real acquisition."""

    approved: FileState | None
    before: FileState | None
    held: FileState
    identity: tuple[int, int]
    check: Callable[[], tuple[int, int]]
