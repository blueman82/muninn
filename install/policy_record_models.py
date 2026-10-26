"""Exact JSON-shaped contracts for installer policy recovery snapshots."""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Field,
    with_config,
)
from typing_extensions import TypedDict

Nonnegative = Annotated[int, Field(ge=0)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[list[Nonnegative], Field(min_length=2, max_length=2)]
Directory = Annotated[list[Nonnegative], Field(min_length=3, max_length=3)]


def _utf8(value: str) -> str:
    """Require bytes that policy digests and profile parsers can encode."""
    value.encode("utf-8")
    return value


Utf8 = Annotated[str, AfterValidator(_utf8)]


def _link(value: list[str | int]) -> list[str | int]:
    """Require the exact text/device/inode list without coercing its slots."""
    if (
        len(value) != 3
        or not isinstance(value[0], str)
        or any(type(item) is not int or item < 0 for item in value[1:])
    ):
        raise ValueError("invalid_link_binding")
    return value


Link = Annotated[list[str | int], AfterValidator(_link)]


def _time(value: object) -> float:
    """Keep the bootstrap clock's strict floating-point contract."""
    if type(value) is not float:
        raise ValueError("invalid_activation_time")
    return value


@with_config(ConfigDict(strict=True, extra="forbid"))
class FileState(TypedDict):
    """Retained ordinary-file identity, size, clocks and byte digest."""

    stamp: Annotated[list[int], Field(min_length=6, max_length=6)]
    sha256: Digest


@with_config(ConfigDict(strict=True, extra="forbid"))
class LinkState(TypedDict):
    """Runtime link identity and the observed resolved target."""

    link: str
    identity: Identity
    target: FileState | Directory


@with_config(ConfigDict(strict=True, extra="forbid"))
class TreeState(TypedDict):
    """Complete runtime or plugin-cache file and directory observations."""

    directories: dict[str, Directory]
    files: dict[str, FileState | LinkState]


@with_config(ConfigDict(strict=True, extra="forbid"))
class Selection(TypedDict):
    """Stable launcher links and optional plugin-cache identity."""

    links: dict[str, Link]
    cache: TreeState | None


@with_config(ConfigDict(strict=True, extra="forbid"))
class Ready(TypedDict):
    """Completed compatible runtime/configuration before activation."""

    runtime: TreeState
    files: dict[str, FileState | None]
    selection: Selection


@with_config(ConfigDict(strict=True, extra="forbid"))
class Control(TypedDict):
    """Configured control row captured after bootstrap in its transaction."""

    singleton: Annotated[int, Field(ge=1, le=1)]
    generation: Nonnegative
    admission: Annotated[int, Field(ge=0, le=1)]
    policy_json: Utf8
    policy_digest: Digest
    activated_at: Annotated[
        float, BeforeValidator(_time), Field(ge=0, allow_inf_nan=False)
    ]


@with_config(ConfigDict(strict=True, extra="forbid"))
class Intent(TypedDict):
    """Store identity, exact control row and ordered source-boundary digest."""

    store: Identity
    control: Control
    boundaries: Digest


@with_config(ConfigDict(strict=True, extra="forbid"))
class Activation(TypedDict):
    """First-activation intent persisted before COMMIT and its completion."""

    phase: Literal["intent", "complete"]
    intent: Intent


@with_config(ConfigDict(strict=True, extra="forbid"))
class Payload(TypedDict):
    """All-or-nothing raw and canonical policy bytes with their digests."""

    raw: Utf8
    raw_digest: Digest
    canonical: Utf8
    digest: Digest


@with_config(ConfigDict(strict=True, extra="forbid"))
class LegacyDefaultSelection(Payload):
    """Original repository-default fields retained in completed old records."""

    policy_origin: Literal["repository_default"]
    default_executable: str
    default_executable_digest: Digest
    default_auth_home: str
    default_auth_identity: Directory
    default_profile_digest: Digest


@with_config(ConfigDict(strict=True, extra="forbid"))
class DefaultSelection(LegacyDefaultSelection):
    """Current default capability bindings required for materialization."""

    default_executable_target: str
    default_profile: Utf8


@with_config(ConfigDict(strict=True, extra="forbid"))
class InputBinding(TypedDict):
    """Operation and home identity retained before installation disruption."""

    enable_learning: NotRequired[bool]
    status: Literal["activation_pending"]
    operation: str
    home: str
    home_identity: Directory
    sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


@with_config(ConfigDict(strict=True, extra="forbid"))
class SelectedInput(InputBinding, Payload):
    """Explicit custom policy snapshot with operation ownership."""


@with_config(ConfigDict(strict=True, extra="forbid"))
class DefaultInput(InputBinding, DefaultSelection):
    """Repository-default snapshot with complete operation ownership."""


@with_config(ConfigDict(strict=True, extra="forbid"))
class HistoricalDefaultInput(InputBinding, LegacyDefaultSelection):
    """Earlier default captures without newer target or profile bindings."""

    default_executable_target: NotRequired[str]
    default_profile: NotRequired[Utf8]


PolicyInput = (
    InputBinding | SelectedInput | DefaultInput | HistoricalDefaultInput
)


@with_config(ConfigDict(strict=True, extra="allow"))
class RecoveryEnvelope(TypedDict):
    """Run fields used to derive an ordinary activation recovery context."""

    ts: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
    fresh: bool
    upgrade: bool
    sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    home: str
    rdir: str
    repo: str
    install_complete: NotRequired[bool]
    superseded_by: NotRequired[str]
