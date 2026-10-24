"""Name the explicitly accepted limitations of ongoing Mac Codex production."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
from typing import Literal

from pydantic import field_validator

from muninn.knowledge_models import Digest, StrictModel

# Historical assessment proof still uses this hash; ongoing admission does not.
PINNED_EXECUTABLE = (
    "8813367285524ac798dd96d1b071aea30d58708c7226fd236cd275cf1cfdee31"
)


class CodexExperiment(StrictModel):
    """Record a C6 amendment with no preventive isolation claim."""

    kind: Literal["ongoing-mac-codex-v1"]
    activation_authorized: Literal[True]
    host_identity: Digest
    data_home: Path
    accepted_uncertainties: Literal[
        "posthoc-actions;unknown-requests-and-billing;incomplete-tool-source-isolation;deferred-C6-assessment"
    ]

    @field_validator("activation_authorized", mode="before")
    @classmethod
    def explicit_authorization(cls, value: object) -> object:
        """Require literal true in the recorded authorization."""
        if value is not True:
            raise ValueError("codex_experimental_authorization")
        return value


def host_identity() -> str:
    """Bind the selected native host without reading credentials."""
    return hashlib.sha256(
        json.dumps(
            [platform.system(), platform.machine(), platform.node()],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def check_host(terms: CodexExperiment, home: Path) -> None:
    """Refuse activation on another host or under another data home."""
    if (
        platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or terms.host_identity != host_identity()
        or terms.data_home != home
        or not home.is_absolute()
        or home.resolve() != home
    ):
        raise ValueError("codex_experimental_boundary")


def check_activation(terms: CodexExperiment, home: Path) -> None:
    """Require the same bound host and disabled recall before activation."""
    check_host(terms, home)
    if not (home / "recall.off").is_file():
        raise ValueError("review_policy_recall_enabled")
