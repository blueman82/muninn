"""Prepare an unverified static candidate exclusively for the harness."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from muninn.review_json import canonical_json, result_schema
from muninn.review_launch_spec import LaunchSpec, launch_spec
from muninn.review_models import ReviewContext
from muninn.review_output import output_contract_digest
from muninn.review_rounds import check_round, round_schema
from muninn.review_runner import (
    RunnerCapability,
    check_private_path,
    config_fingerprint,
    file_digest,
    read_private_file,
)
from tools.learning_eval.cases import digest, strict_json
from tools.learning_eval.models import TrialAuthorization
from tools.learning_eval.trial_models import Candidate


def candidate_environment(candidate: Candidate, home: Path) -> dict[str, str]:
    """Select only named environment values and bind disposable paths."""
    check_private_path(home, directory=True)
    check_private_path(candidate.cli_cwd, directory=True)
    if home not in candidate.cli_cwd.parents:
        raise ValueError("capability_config")
    if any(
        root != home and home not in root.parents
        for root in candidate.config_roots
    ):
        raise ValueError("capability_config")
    environment: dict[str, str] = {}
    for key, expected in candidate.environment_sha256.items():
        value = os.environ.get(key)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ValueError("capability_config")
        if (
            value is None
            or "\0" in value
            or hashlib.sha256(value.encode()).hexdigest() != expected
        ):
            raise ValueError("capability_changed")
        environment[key] = value
    if environment.get("HOME") != str(home):
        raise ValueError("capability_config")
    return environment


def prepare_capability_trial(
    context: ReviewContext,
    capability: RunnerCapability,
    prompt: bytes,
    authorization: TrialAuthorization,
) -> LaunchSpec:
    """Validate exact candidate bytes without reserving or starting a call."""
    context = ReviewContext.model_validate_json(canonical_json(context))
    capability = RunnerCapability.model_validate_json(
        canonical_json(capability)
    )
    authorization = TrialAuthorization.model_validate_json(
        canonical_json(authorization)
    )
    check_round(context)
    if (
        os.name != "posix"
        or capability.runner != "claude_cli"
        or capability.status == "unsupported"
    ):
        raise ValueError("runner_unsupported")
    if (
        type(prompt) is not bytes
        or len(prompt) > context.policy.max_input_bytes
    ):
        raise ValueError("invalid_prompt_size")
    check_private_path(capability.proof_path, directory=False)
    raw = read_private_file(capability.proof_path, 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != authorization.candidate_digest:
        raise ValueError("capability_changed")
    strict_json(raw)
    candidate = Candidate.model_validate_json(raw)
    if (
        candidate.assessment_id != authorization.assessment_id
        or candidate.budget_account_id != authorization.budget.account_id
        or context.policy.budget.account_id != authorization.budget.account_id
        or context.policy.budget.currency != authorization.budget.currency
    ):
        raise ValueError("authorization_mismatch")
    requested = context.policy.runner
    if (
        candidate.requested != requested
        or any(
            getattr(capability, key) != value
            for key, value in requested.model_dump().items()
        )
        or candidate.schema_digest != capability.schema_digest
        or candidate.schema_digest != digest(round_schema())
        or context.policy.result_schema_digest != digest(result_schema())
        or candidate.output_contract_digest != output_contract_digest()
        or candidate.config_digest != capability.config_digest
        or candidate.config_digest != authorization.config_digest
    ):
        raise ValueError("capability_changed")
    environment = candidate_environment(
        candidate, authorization.disposable_home
    )
    hashed, _ = file_digest(Path(requested.executable), 256 * 1024 * 1024)
    if (
        hashed != candidate.executable_sha256
        or not os.access(requested.executable, os.X_OK)
        or config_fingerprint(
            candidate.config_roots,
            candidate.environment_sha256,
            candidate.cli_cwd,
        )
        != candidate.config_digest
    ):
        raise ValueError("capability_changed")
    check_round(context)
    return launch_spec(context, candidate.cli_cwd, environment, prompt)
