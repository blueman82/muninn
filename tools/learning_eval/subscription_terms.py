"""Validate offline subscription declarations before harness account entry."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from muninn.knowledge_models import (
    Digest,
    SchemaVersion,
    StrictModel,
    Text,
    UtcSeconds,
)
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import PilotBudget, PositiveSeconds, RunnerConfig
from muninn.review_runner import check_private_path, read_private_file
from tools.learning_eval.account import Account
from tools.learning_eval.cases import digest
from tools.learning_eval.models import Assessment
from tools.learning_eval.native_terms import authority_bytes
from tools.learning_eval.pilot_evidence import immutable
from tools.learning_eval.trial_models import checked_assessment


class SubscriptionTerms(StrictModel):
    """Bind a first characterization without granting native launch access."""

    schema_version: SchemaVersion
    kind: Literal["codex-subscription-characterization-v1"]
    purpose: Literal["characterization_only"]
    auth_method: Literal["chatgpt"]
    assessment_id: Text
    budget: PilotBudget
    account_path: Path
    receipt_path: Path
    owner_boundary_path: Path
    owner_boundary_digest: Digest
    started_at: UtcSeconds
    deadline_at: UtcSeconds
    per_call_seconds: PositiveSeconds
    candidate_digest: Digest
    host_identity: Digest
    auth_context_digest: Digest
    executable_sha256: Digest
    schema_digest: Digest
    decoder_digest: Digest
    config_digest: Digest
    requested: RunnerConfig

    @model_validator(mode="after")
    def characterization_limits(self) -> Self:
        """Refuse monetary assumptions and enlargement of the first window."""
        budget = self.budget
        if (
            budget.max_model_calls > 2
            or budget.max_wall_seconds > 600
            or budget.max_cost_decimal is not None
            or budget.conservative_call_cost_decimal is not None
            or budget.currency is not None
            or self.deadline_at <= self.started_at
            or self.deadline_at - self.started_at > budget.max_wall_seconds
            or self.started_at < budget.approved_at
            or self.per_call_seconds > 90
            or self.requested.runner != "codex_cli"
        ):
            raise ValueError("subscription_characterization_limits")
        paths = (
            self.account_path,
            self.receipt_path,
            self.owner_boundary_path,
        )
        if (
            any(
                not path.is_absolute() or path.resolve() != path
                for path in paths
            )
            or self.receipt_path == self.owner_boundary_path
            or self.account_path == self.receipt_path
            or self.account_path in self.receipt_path.parents
            or self.account_path == self.owner_boundary_path
            or self.account_path in self.owner_boundary_path.parents
        ):
            raise ValueError("subscription_account_location")
        return self


class SubscriptionSelection(StrictModel):
    """Carry independently supplied expectations, never observed auth proof."""

    terms_digest: Digest
    candidate_digest: Digest
    host_identity: Digest
    auth_context_digest: Digest
    requested: RunnerConfig
    auth_method: Literal["chatgpt", "api", "unknown"]


def validate_terms(
    terms: SubscriptionTerms,
    assessment: Assessment,
    selection: SubscriptionSelection,
    now: float,
) -> None:
    """Refuse mismatched or expired declarations without opening an account."""
    terms = SubscriptionTerms.model_validate_json(canonical_json(terms))
    selection = SubscriptionSelection.model_validate_json(
        canonical_json(selection)
    )
    assessment = checked_assessment(assessment)
    if (
        type(now) not in (int, float)
        or not math.isfinite(now)
        or now < terms.started_at
        or now >= terms.deadline_at
    ):
        raise ValueError("subscription_window_refused")
    if (
        selection.auth_method != "chatgpt"
        or selection.terms_digest != digest(terms.model_dump(mode="json"))
        or selection.candidate_digest != terms.candidate_digest
        or selection.host_identity != terms.host_identity
        or selection.auth_context_digest != terms.auth_context_digest
        or selection.requested != terms.requested
        or terms.assessment_id != assessment.id
        or terms.budget != assessment.budget
        or terms.config_digest != assessment.config_digest
        or terms.budget.approved_by != assessment.approved_by
        or terms.budget.approved_at != assessment.approved_at
        or assessment.phase != "pilot"
    ):
        raise ValueError("subscription_binding_mismatch")
    for policy in assessment.policies:
        if (
            policy.runner != terms.requested
            or policy.budget != terms.budget
            or policy.max_attempts != 1
            or policy.concurrency != 1
            or policy.max_rounds != 1
            or policy.deadline_seconds > terms.per_call_seconds
        ):
            raise ValueError("subscription_policy_mismatch")
    authority_bytes(terms.owner_boundary_path, terms.owner_boundary_digest)


def _bind_receipt(
    terms: SubscriptionTerms, account: Account, initialize: bool
) -> None:
    """Keep the terms and database identity outside a replaceable account."""
    info = (account.path / "account.sqlite3").stat()
    raw = canonical_json_value(
        {
            "terms_digest": digest(terms.model_dump(mode="json")),
            "account_device": info.st_dev,
            "account_inode": info.st_ino,
        }
    ).encode()
    hashed = hashlib.sha256(raw).hexdigest()
    prior = account.result_binding("subscription-terms")
    if (initialize and prior is not None) or (
        not initialize and prior != hashed
    ):
        raise ValueError("subscription_account_reset")
    if (
        not initialize
        and read_private_file(terms.receipt_path, 1024 * 1024) != raw
    ):
        raise ValueError("subscription_account_reset")
    immutable(
        terms.receipt_path.parent, terms.receipt_path.name, raw, 1024 * 1024
    )
    account.result_binding("subscription-terms", hashed)


@contextmanager
def subscription_account(
    terms: SubscriptionTerms,
    assessment: Assessment,
    selection: SubscriptionSelection,
    *,
    initialize: bool = False,
) -> Generator[Account]:
    """Validate before ownership and refuse cleanup without signals."""
    validate_terms(terms, assessment, selection, time.time())
    check_private_path(terms.account_path, directory=True)
    check_private_path(terms.receipt_path.parent, directory=True)
    if initialize == terms.receipt_path.exists():
        raise ValueError("subscription_account_reset")
    with Account(
        terms.account_path,
        assessment,
        initialize=initialize,
        refuse_unclean=True,
    ) as account:
        validate_terms(terms, assessment, selection, time.time())
        _bind_receipt(terms, account, initialize)
        yield account
