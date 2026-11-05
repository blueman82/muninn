"""Define exact initial managed-interface plans and durable phase receipts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from install.host_renewal_models import ContainmentPlan, HostState
from install.policy_record_models import Control
from muninn.knowledge_models import Digest, StrictModel, Text, UtcSeconds
from muninn.review_codex_launch import ExperimentalBinding
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_runtime_models import (
    InitialBridge,
    ManagedCodexSelection,
    RunnerConfig,
    RuntimeIdentity,
)


class InterfacePlan(StrictModel):
    """Bind the complete old queue to one proposed stable managed rule."""

    kind: Literal["codex-managed-interface-plan-v1"]
    home: Path
    old: ReviewPolicy
    candidate: ReviewPolicy
    old_binding: ExperimentalBinding
    observation: RuntimeIdentity
    bridge: InitialBridge
    state: HostState
    containment: ContainmentPlan
    minimum_runtime: Literal["codex-managed-interface-v1"]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Require the exact selection delta and its corresponding capsule."""
        old, new = self.old, self.candidate
        if (
            not isinstance(old.runner, RunnerConfig)
            or not isinstance(new.runner, ManagedCodexSelection)
            or old.experimental_codex is None
            or old.model_dump(exclude={"runner"})
            != new.model_dump(exclude={"runner"})
            or (old.runner.model, old.runner.reasoning)
            != (new.runner.model, new.runner.reasoning)
            or self.old_binding.requested != old.runner
            or self.old_binding.host_identity
            != old.experimental_codex.host_identity
            or self.home != old.experimental_codex.data_home
            or self.bridge.home != self.home
            or self.bridge.old_policy_digest
            != hashlib.sha256(canonical_json(old).encode()).hexdigest()
            or self.bridge.new_policy_digest
            != hashlib.sha256(canonical_json(new).encode()).hexdigest()
        ):
            raise ValueError("codex_interface_plan_changed")
        return self

    @property
    def digest(self) -> str:
        """Identify every disclosed plan term without a recursive field."""
        return hashlib.sha256(canonical_json(self).encode()).hexdigest()


class InterfaceApproval(StrictModel):
    """Record fresh affirmative owner consent to one exact stable rule."""

    plan_digest: Digest
    approved_at: UtcSeconds
    authority: Literal["installer_prompt_yes"]


class InterfaceReceipt(StrictModel):
    """Retain exact activation intent and classify its commit after a crash."""

    kind: Literal["codex-managed-interface-receipt-v1"]
    phase: Literal[
        "approved_intent",
        "contained",
        "commit_pending",
        "grant_committed",
        "complete",
    ]
    operation: Text
    home: Path
    repo: Path
    sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    plan: InterfacePlan
    approval: InterfaceApproval
    operation_identity: tuple[int, int]
    owner_control: Control | None = None
    predecessor: Control | None
    successor: Control | None
    contained: HostState | None
    artifacts_digest: Digest
    minimum_runtime: Literal["codex-managed-interface-v1"]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Reject incomplete phases or a widened grant."""
        if self.approval.plan_digest != self.plan.digest:
            raise ValueError("codex_interface_record_changed")
        if self.phase != "approved_intent" and self.contained is None:
            raise ValueError("codex_interface_record_changed")
        if self.phase in {"commit_pending", "grant_committed", "complete"}:
            if (
                self.contained is None
                or self.predecessor is None
                or self.predecessor != self.contained.control
                or self.successor is None
            ):
                raise ValueError("codex_interface_record_changed")
            expected = self.predecessor | {
                "policy_json": canonical_json(self.plan.candidate),
                "policy_digest": hashlib.sha256(
                    canonical_json(self.plan.candidate).encode()
                ).hexdigest(),
                "generation": self.predecessor["generation"] + 1,
            }
            if self.successor != expected:
                raise ValueError("codex_interface_record_changed")
        elif self.predecessor is not None or self.successor is not None:
            raise ValueError("codex_interface_record_changed")
        return self
