"""Separate stable runner authority from observed per-call execution facts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from muninn.knowledge_models import Digest, StrictModel, Text, UtcSeconds


class RunnerConfig(StrictModel):
    """Require every requested legacy runner setting explicitly."""

    runner: Literal["claude_cli", "codex_cli"]
    executable: Text
    version: Text
    model: Text
    reasoning: Text


class ManagedCodexSelection(StrictModel):
    """Bind a trusted manager and interface without approving a version."""

    selection: Literal["codex_managed_interface_v1"]
    runner: Literal["codex_cli"]
    manager_root: Path
    selector: Literal["current"]
    interface: Literal["codex-json-review-v1"]
    model: Text
    reasoning: Text


class FileStamp(StrictModel):
    """Record immutable identity checks for one package path."""

    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    uid: int
    gid: int
    link_target: Text | None


class RuntimeIdentity(StrictModel):
    """Describe checked execution facts without granting authority."""

    requested: RunnerConfig
    package_root: Path
    executable_sha256: Digest
    package_sha256: Digest
    platform: Literal["Darwin/arm64"]
    selector_stamp: FileStamp
    file_stamps: Annotated[
        tuple[FileStamp, ...], Field(min_length=1, max_length=256)
    ]
    recipe_digest: Digest
    schema_digest: Digest
    validator_digest: Digest
    capability_key: Digest

    @property
    def digest(self) -> str:
        """Identify checked facts without adding a serialization field."""
        raw = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(raw.encode()).hexdigest()


class ActivationEvent(StrictModel):
    """Record an immutable activation offer or positive pre-exec refusal."""

    kind: Literal["offer", "prestart_refusal"]
    identity_digest: Digest
    code: Literal["runtime_changed", "exec_enoent"] | None
    at: UtcSeconds

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Require a finite cause only for a positive refusal."""
        if (self.kind == "prestart_refusal") != (self.code is not None):
            raise ValueError("runtime_record_invalid")
        return self


class ManagedCallRecord(StrictModel):
    """Preserve stable selection and append-only actual call candidates."""

    record: Literal["codex-managed-call-v1"]
    selection: ManagedCodexSelection
    candidates: Annotated[
        tuple[RuntimeIdentity, ...], Field(min_length=1, max_length=2)
    ]
    events: Annotated[tuple[ActivationEvent, ...], Field(max_length=4)] = ()
    refresh_count: Literal[0, 1] = 0
    launched_identity: Digest | None = None

    @model_validator(mode="after")
    def coherent_history(self) -> Self:
        """Reject invented launches, duplicate candidates and bad budgets."""
        identities = tuple(candidate.digest for candidate in self.candidates)
        if len(set(identities)) != len(identities) or (
            len(identities) == 2 and self.refresh_count != 1
        ):
            raise ValueError("runtime_record_invalid")
        for candidate in self.candidates:
            requested = candidate.requested
            correct_model = (
                requested.runner,
                requested.model,
                requested.reasoning,
            ) == (
                "codex_cli",
                self.selection.model,
                self.selection.reasoning,
            )
            correct_root = (
                candidate.package_root.parent
                == self.selection.manager_root / "releases"
            )
            if not correct_model or not correct_root:
                raise ValueError("runtime_record_invalid")
        for index, event in enumerate(self.events):
            expected = "offer" if index % 2 == 0 else "prestart_refusal"
            if (
                event.kind != expected
                or event.identity_digest not in identities
            ):
                raise ValueError("runtime_record_invalid")
            if (
                expected == "prestart_refusal"
                and event.identity_digest
                != self.events[index - 1].identity_digest
            ):
                raise ValueError("runtime_record_invalid")
        offered = tuple(
            event.identity_digest
            for event in self.events
            if event.kind == "offer"
        )
        if offered and offered not in ((identities[-1],), identities):
            raise ValueError("runtime_record_invalid")
        if self.launched_identity is not None and (
            not self.events
            or self.events[-1].kind != "offer"
            or self.events[-1].identity_digest != self.launched_identity
        ):
            raise ValueError("runtime_record_invalid")
        return self


class ManagedCapability(StrictModel):
    """Select an activated stable managed proof without runtime authority."""

    kind: Literal["codex-managed-capability-v1"]
    requested: ManagedCodexSelection
    proof_path: Path
    schema_digest: Digest
    config_digest: Digest


class ManagedInterfaceBinding(StrictModel):
    """Bind host, original auth and immutable initial enrollment approval."""

    kind: Literal["codex-managed-binding-v1"]
    requested: ManagedCodexSelection
    host_identity: Digest
    auth_home: Path
    cli_cwd: Path
    schema_path: Path
    output_contract_digest: Digest
    enrollment_approval: Digest
    initial_bridge: Digest | None


type RunnerSelection = RunnerConfig | ManagedCodexSelection


class EnrollmentApproval(StrictModel):
    """Keep the exact one-time owner decision separate from runtime facts."""

    kind: Literal["codex-managed-enrollment-v1"]
    old_policy_digest: Digest
    new_policy_digest: Digest
    bridge_digest: Digest | None
    host_identity: Digest
    auth_home: Path
    operation_id: Text
    plan_digest: Digest
    approved_at: UtcSeconds
    approval: Literal["owner_yes"]


class ActivationRuntime(StrictModel):
    """Carry the one round refresh budget with a checked execution offer."""

    execution: RuntimeIdentity
    refresh_count: Literal[0, 1] = 0
    replacement: bool = False


class BridgeRequest(StrictModel):
    """Identify one immutable old request and its complete original inputs."""

    request_id: Text
    metadata_digest: Digest
    inputs: Annotated[tuple[tuple[Text, Digest], ...], Field(max_length=4096)]


class InitialBridge(StrictModel):
    """Authorize one finite old queue under a single new stable contract."""

    kind: Literal["codex-managed-initial-bridge-v1"]
    home: Path
    old_policy_digest: Digest
    new_policy_digest: Digest
    requests: Annotated[tuple[BridgeRequest, ...], Field(max_length=1024)]

    @model_validator(mode="after")
    def unique_requests(self) -> Self:
        """Reject duplicate queue identifiers."""
        ids = tuple(item.request_id for item in self.requests)
        if len(ids) != len(set(ids)):
            raise ValueError("review_policy_required")
        return self


def require_legacy_runner(selection: RunnerSelection) -> RunnerConfig:
    """Refuse managed authority at a legacy-only exact executable recipe."""
    if not isinstance(selection, RunnerConfig):
        raise ValueError("runtime_interface_unsupported")
    return selection
