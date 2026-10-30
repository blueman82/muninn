"""Strict digest-bound host-renewal plans and durable predecessor receipts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator

from install.host_renewal_binding_models import BindingInputs
from install.policy_record_models import (
    Control,
    FileState,
    Identity,
    Payload,
    Ready,
)
from muninn.knowledge_models import (
    AttemptId,
    Digest,
    RequestId,
    SchemaVersion,
    StrictModel,
    UtcSeconds,
)
from muninn.review_json import (
    canonical_json_value,
    reject_constant,
    unique_object,
)
from muninn.review_models import AttemptView, RequestView, ReviewPolicy
from muninn.review_pause_state import Witness
from muninn.review_state import LearningSelection


def _float(value: object) -> object:
    """Reject coerced consent and containment clocks before validation."""
    if type(value) is not float:
        raise ValueError("host_renewal_time_invalid")
    return value


def _bool(value: object) -> object:
    """Require actual JSON booleans rather than numerically equal values."""
    if type(value) is not bool:
        raise ValueError("host_renewal_boolean_invalid")
    return value


def _path(value: object) -> str:
    """Check canonical absolute spelling without reading the filesystem."""
    if not isinstance(value, str):
        raise ValueError("host_renewal_path_invalid")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("host_renewal_path_invalid")
    return value


def _payload(value: Payload) -> ReviewPolicy:
    """Require exact canonical policy bytes and both retained digests."""
    json.loads(
        value["raw"],
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    policy = ReviewPolicy.model_validate_json(value["raw"])
    canonical = canonical_json_value(policy.model_dump(mode="json"))
    if (
        canonical != value["canonical"]
        or hashlib.sha256(value["raw"].encode()).hexdigest()
        != value["raw_digest"]
        or hashlib.sha256(canonical.encode()).hexdigest() != value["digest"]
    ):
        raise ValueError("host_renewal_payload_invalid")
    return policy


def _integer(value: object) -> object:
    """Reject boolean and floating versions that compare equal to integers."""
    if type(value) is not int:
        raise ValueError("host_renewal_version_invalid")
    return value


type Clock = Annotated[UtcSeconds, BeforeValidator(_float)]
type AbsolutePath = Annotated[str, Field(min_length=1), BeforeValidator(_path)]
type Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
type Operation = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]


class ContainmentJob(StrictModel):
    """Bind one exact unfinished predecessor and its complete attempt count."""

    request: RequestView
    attempt: AttemptView
    attempt_count: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def linkage(self) -> Self:
        """Retain exact request linkage after the current pointer clears."""
        if self.attempt.request_id != self.request.id:
            raise ValueError("host_renewal_job_invalid")
        if self.attempt.ended_at is None and self.request.state not in {
            "running",
            "cancelled",
        }:
            raise ValueError("host_renewal_job_invalid")
        return self


class ContainmentPlan(StrictModel):
    """Project named no-call jobs and all unchanged durable invariants."""

    jobs: tuple[ContainmentJob, ...]
    invariant_digest: Digest
    setup_before_digest: Digest | None
    witness: Witness

    @model_validator(mode="after")
    def unique_jobs(self) -> Self:
        """Reject duplicate or reordered predecessor jobs."""
        ids = tuple(job.request.id for job in self.jobs)
        attempts = tuple(job.attempt.id for job in self.jobs)
        if ids != tuple(sorted(set(ids))) or len(attempts) != len(
            set(attempts)
        ):
            raise ValueError("host_renewal_job_invalid")
        return self


class HostState(StrictModel):
    """Bind exact control separately from complete unchanged-state groups."""

    control: Control
    learning: LearningSelection | None
    progress_digest: Digest
    work_digest: Digest
    erasure_digest: Digest
    store_identity: Identity


class HostPlan(StrictModel):
    """Retain the complete checked proposal without conferring consent."""

    kind: Literal["codex_host_renewal_v1"]
    home: AbsolutePath
    repo: AbsolutePath
    sha: Sha
    old: Payload
    candidate: Payload
    state: HostState
    bindings_digest: Digest
    artifacts_digest: Digest
    operation_lock: FileState | None
    request_ids: tuple[RequestId, ...]
    attempt_ids: tuple[AttemptId, ...]
    containment: ContainmentPlan
    digest: Digest

    @model_validator(mode="after")
    def checked(self) -> Self:
        """Reject inconsistent payloads, job inventories and self-digests."""
        old, candidate = _payload(self.old), _payload(self.candidate)
        before, after = old.experimental_codex, candidate.experimental_codex
        if (
            before is None
            or after is None
            or before.host_identity == after.host_identity
        ):
            raise ValueError("host_renewal_policy_changed")
        expected = old.model_dump(mode="json")
        expected["experimental_codex"]["host_identity"] = after.host_identity
        if expected != candidate.model_dump(mode="json"):
            raise ValueError("host_renewal_policy_changed")
        for ids in (self.request_ids, self.attempt_ids):
            if ids != tuple(sorted(set(ids))):
                raise ValueError("host_renewal_ids_invalid")
        if any(
            job.request.id not in self.request_ids
            or job.attempt.id not in self.attempt_ids
            for job in self.containment.jobs
        ):
            raise ValueError("host_renewal_job_invalid")
        control = self.state.control
        if (
            control["policy_json"] != self.old["canonical"]
            or control["policy_digest"] != self.old["digest"]
        ):
            raise ValueError("host_renewal_control_invalid")
        fields = self.model_dump(mode="json", exclude={"digest"})
        if (
            hashlib.sha256(canonical_json_value(fields).encode()).hexdigest()
            != self.digest
        ):
            raise ValueError("host_renewal_plan_invalid")
        return self


class HostApproval(StrictModel):
    """Record an actual affirmative installer prompt for one concrete plan."""

    plan_digest: Digest
    approved_at: Clock
    authority: Literal["installer_prompt"]
    confirmed: Annotated[Literal[True], BeforeValidator(_bool)]


class SetupNotice(StrictModel):
    """Bind the approved setup code and complete row fingerprint."""

    code: Literal["codex_host_changed", "codex_experimental_boundary"]
    row_digest: Digest


class ReceiptBase(StrictModel):
    """Retain operation authority shared by every recoverable phase."""

    version: SchemaVersion
    operation: Operation
    home: AbsolutePath
    repo: AbsolutePath
    sha: Sha
    plan: HostPlan
    approval: HostApproval
    operation_identity: Identity
    held_operation_lock: FileState
    bindings: BindingInputs
    setup_notice: SetupNotice | None
    prior_summary: FileState | None
    recognized_summary: FileState | None
    containment_started_at: Clock

    @model_validator(mode="after")
    def authority(self) -> Self:
        """Require exact selectors and the separately approved plan."""
        if (
            (self.home, self.repo, self.sha)
            != (self.plan.home, self.plan.repo, self.plan.sha)
            or self.approval.plan_digest != self.plan.digest
            or self.bindings.digest() != self.plan.bindings_digest
            or self.held_operation_lock["stamp"][:2] != self.operation_identity
            or self.containment_started_at < self.approval.approved_at
        ):
            raise ValueError("host_renewal_authority_invalid")
        if self.setup_notice is not None and (
            self.setup_notice.row_digest
            != self.plan.containment.setup_before_digest
        ):
            raise ValueError("host_renewal_setup_changed")
        return self


def _control_step(
    before: Control, after: Control, increments: tuple[int, ...]
) -> None:
    """Require only the exact generation increment and monotone admission."""
    expected = before | {
        "generation": after["generation"],
        "admission": after["admission"],
    }
    if (
        after != expected
        or after["generation"] - before["generation"] not in increments
        or after["admission"] > before["admission"]
    ):
        raise ValueError("host_renewal_control_invalid")


class ApprovedIntent(ReceiptBase):
    """Preserve approved intent and an optional actual owner observation."""

    phase: Literal["approved_intent"]
    owner_control: Control | None = None

    @model_validator(mode="after")
    def owner_observation(self) -> Self:
        """Check a partial owner observation without claiming containment."""
        if self.owner_control is not None:
            _control_step(self.plan.state.control, self.owner_control, (0, 1))
        return self


class ContainedFacts(ReceiptBase):
    """Require completed containment and its confined complete raw archive."""

    contained: HostState
    archive: FileState
    archive_path: AbsolutePath
    archive_format: Annotated[Literal[2], BeforeValidator(_integer)]
    archive_status: Literal["complete"]
    owner_control: Control
    containment_finished_at: Clock

    @model_validator(mode="after")
    def archive_bound(self) -> Self:
        """Refuse backward clocks and archives outside this operation root."""
        root = (
            Path(self.home)
            / ".local/share"
            / ("muninn-install-" + self.operation)
        )
        if (
            Path(self.archive_path).parent != root
            or self.containment_finished_at < self.containment_started_at
        ):
            raise ValueError("host_renewal_archive_invalid")
        _control_step(self.plan.state.control, self.owner_control, (0, 1))
        _control_step(self.owner_control, self.contained.control, (1,))
        for key in (
            "learning",
            "progress_digest",
            "erasure_digest",
            "store_identity",
        ):
            if getattr(self.contained, key) != getattr(self.plan.state, key):
                raise ValueError("host_renewal_state_changed")
        return self


class PendingFacts(ContainedFacts):
    """Require the exact predecessor and prepared runtime before grant."""

    predecessor: Control
    successor: Control
    ready: Ready

    @model_validator(mode="after")
    def pending_controls(self) -> Self:
        """Require the exact contained predecessor and one host-only grant."""
        candidate = self.plan.candidate
        expected = self.predecessor | {
            "generation": self.predecessor["generation"] + 1,
            "policy_json": candidate["canonical"],
            "policy_digest": candidate["digest"],
        }
        if (
            self.predecessor != self.contained.control
            or self.successor != expected
        ):
            raise ValueError("host_renewal_control_invalid")
        return self


class ContainedArchived(ContainedFacts):
    """Retain the complete observed containment and archive phase."""

    phase: Literal["contained_archived"]


class CommitPending(PendingFacts):
    """Retain exact pending predecessor and successor facts."""

    phase: Literal["commit_pending"]


class GrantCommitted(PendingFacts):
    """Retain the exact durable pending facts after the policy COMMIT."""

    phase: Literal["grant_committed"]


class RestartPending(PendingFacts):
    """Keep a committed grant distinguishable from incomplete restart."""

    phase: Literal["restart_pending"]


class VerificationOutcome(StrictModel):
    """Retain bounded actual installer check outcomes without detail text."""

    check: Literal[
        "runtime_bootstrap",
        "hook_runs",
        "doctor",
        "new_pid_alive",
        "heartbeat_fresh",
        "codex_resolves_new_hooks",
        "codex_hook_hashes_match",
        "managed_service_verified",
    ]
    ok: Annotated[bool, BeforeValidator(_bool)] | None


class UpgradeVerified(PendingFacts):
    """Retain successful core verification and actual optional outcomes."""

    phase: Literal["upgrade_verified"]
    verification: Annotated[
        tuple[VerificationOutcome, ...], Field(min_length=5, max_length=8)
    ]

    @model_validator(mode="after")
    def outcomes(self) -> Self:
        """Require unique sorted actual checks and all five core successes."""
        names = tuple(item.check for item in self.verification)
        core = {
            "runtime_bootstrap",
            "hook_runs",
            "doctor",
            "new_pid_alive",
            "heartbeat_fresh",
        }
        if (
            names != tuple(sorted(set(names)))
            or any(
                item.ok is False
                or (item.check in core and item.ok is not True)
                for item in self.verification
            )
            or not core <= set(names)
        ):
            raise ValueError("host_renewal_verification_invalid")
        return self


type RenewalReceipt = Annotated[
    ApprovedIntent
    | ContainedArchived
    | CommitPending
    | GrantCommitted
    | RestartPending
    | UpgradeVerified,
    Field(discriminator="phase"),
]
RECEIPT = TypeAdapter(RenewalReceipt)
