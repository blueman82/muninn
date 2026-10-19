"""Prepare an unverified static candidate exclusively for the harness."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from muninn import review_codex_output
from muninn.review_json import canonical_json, result_schema
from muninn.review_launch_inputs import PromptArtifact
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
from tools.learning_eval.native_codex import (
    native_environment,
    native_fingerprint,
    require_json_instruction,
    validate_native_prompt,
    wire_schema,
)
from tools.learning_eval.native_terms import validate_terms
from tools.learning_eval.pilot_models import PilotManifest
from tools.learning_eval.task_protocol import protocol_schema, task_schema
from tools.learning_eval.trial_models import (
    Candidate,
    ScheduledCall,
    checked_assessment,
    validate_schedule,
)


def candidate_environment(candidate: Candidate, home: Path) -> dict[str, str]:
    """Select only named environment values and bind disposable paths."""
    if candidate.kind == "native_codex_trial":
        return native_environment(candidate, home)
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
    protocol: str = "muninn-review-round-v1",
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
        or capability.runner not in {"claude_cli", "codex_cli"}
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
    if candidate.kind == "native_codex_trial":
        validate_native_prompt(prompt, protocol)
    if (
        candidate.assessment_id != authorization.assessment_id
        or candidate.budget_account_id != authorization.budget.account_id
        or context.policy.budget.account_id != authorization.budget.account_id
        or context.policy.budget.currency != authorization.budget.currency
    ):
        raise ValueError("authorization_mismatch")
    if protocol not in {"muninn-review-round-v1", "muninn-task-answer-v1"}:
        raise ValueError("protocol_mismatch")
    schema = (
        round_schema()
        if protocol == "muninn-review-round-v1"
        else task_schema()
    )
    if candidate.kind == "native_codex_trial":
        schema = wire_schema()
    requested = context.policy.runner
    if (
        candidate.protocol != protocol
        or candidate.requested != requested
        or any(
            getattr(capability, key) != value
            for key, value in requested.model_dump().items()
        )
        or candidate.schema_digest != capability.schema_digest
        or candidate.schema_digest != digest(schema)
        or context.policy.result_schema_digest != digest(result_schema())
        or candidate.output_contract_digest
        != candidate_output_digest(candidate)
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
        or candidate_config_digest(candidate) != candidate.config_digest
    ):
        raise ValueError("capability_changed")
    check_round(context)
    return launch_spec(
        context,
        candidate.cli_cwd,
        environment,
        prompt,
        schema,
        schema_path=candidate.schema_path,
    )


def validate_pilot_manifest(manifest: PilotManifest) -> bytes:
    """Validate phase, budget and candidates before reading cases."""
    assessment = checked_assessment(manifest.assessment)
    native = _manifest_origin(manifest)
    if (
        assessment.phase != "pilot"
        or len(assessment.policies) != 1
        or assessment.heldout_family_digest != manifest.reserved_heldout_digest
        or assessment.budget.max_cost_decimal is None
        or assessment.budget.conservative_call_cost_decimal is None
        or set(manifest.learning) != set(assessment.comparators)
        or set(manifest.task) != set(assessment.comparators)
    ):
        raise ValueError("pilot_manifest_binding")
    for path in (manifest.account_path, manifest.artifact_path):
        check_private_path(path, directory=True)
    if manifest.account_path == manifest.artifact_path:
        raise ValueError("pilot_path_binding")
    policy = assessment.policies[0]
    if any(
        r.context_bytes > policy.context_bytes
        or r.guide_bytes > policy.max_input_bytes
        or r.search_limit > 200
        or r.guide_open_limit > 200
        for r in manifest.exposures.values()
    ):
        raise ValueError("pilot_exposure_limit")
    for purpose, bindings in (
        ("learning", manifest.learning),
        ("task", manifest.task),
    ):
        protocol = (
            "muninn-review-round-v1"
            if purpose == "learning"
            else "muninn-task-answer-v1"
        )
        for comparator, binding in bindings.items():
            auth, capability = binding.authorization, binding.capability
            check_private_path(capability.proof_path, directory=False)
            raw = read_private_file(capability.proof_path, 1024 * 1024)
            strict_json(raw)
            candidate = Candidate.model_validate_json(raw)
            _candidate_origin(candidate, manifest, native)
            environment = candidate_environment(
                candidate, auth.disposable_home
            )
            if (
                hashlib.sha256(raw).hexdigest() != auth.candidate_digest
                or candidate.protocol != protocol
                or capability.status != "unverified"
                or candidate.output_contract_digest
                != candidate_output_digest(candidate)
                or candidate.schema_digest
                != digest(
                    wire_schema() if native else protocol_schema(protocol)
                )
                or capability.schema_digest != candidate.schema_digest
                or candidate.requested != policy.runner
                or candidate.assessment_id != assessment.id
                or candidate.budget_account_id != assessment.budget.account_id
                or candidate.config_digest != capability.config_digest
                or candidate.config_digest != auth.config_digest
                or candidate.config_digest
                != candidate_config_digest(candidate)
                or candidate.executable_sha256
                != file_digest(
                    Path(policy.runner.executable), 256 * 1024 * 1024
                )[0]
                or any(
                    getattr(capability, key) != value
                    for key, value in policy.runner.model_dump().items()
                )
                or digest(
                    policy.runner.model_dump(
                        mode="json", exclude={"executable"}
                    )
                )
                != assessment.comparator_config_digests[comparator]
                or not environment
            ):
                raise ValueError("pilot_candidate_binding")
            from_schedule = _validation_schedule(manifest, comparator, purpose)
            validate_schedule(
                assessment, manifest.account_path, from_schedule, auth
            )
            if any(
                path == auth.disposable_home
                or auth.disposable_home in path.parents
                for path in (manifest.account_path, manifest.artifact_path)
            ):
                raise ValueError("pilot_path_binding")
    return _pilot_prompt(manifest)


def _validation_schedule(
    manifest: PilotManifest, comparator: str, purpose: str
) -> ScheduledCall:
    """Check the binding with the existing authority validator."""
    bindings = manifest.learning if purpose == "learning" else manifest.task
    binding = next(v for k, v in bindings.items() if k == comparator)
    return ScheduledCall.model_validate_json(
        json.dumps(
            {
                "id": "A_" + "0" * 32,
                "assessment_id": manifest.assessment.id,
                "comparator": comparator,
                "trial_id": "validation",
                "retry_index": 1,
                "round_index": 1,
                "purpose": purpose,
                "protocol": (
                    "muninn-review-round-v1"
                    if purpose == "learning"
                    else "muninn-task-answer-v1"
                ),
                "schema_digest": digest(
                    protocol_schema(
                        "muninn-review-round-v1"
                        if purpose == "learning"
                        else "muninn-task-answer-v1"
                    )
                ),
                "policy_digest": digest(
                    manifest.assessment.policies[0].model_dump(mode="json")
                ),
                "config_digest": manifest.assessment.config_digest,
                "authorization_digest": digest(
                    binding.authorization.model_dump(mode="json")
                ),
            }
        )
    )


def candidate_output_digest(candidate: Candidate) -> str:
    """Select the decoder contract from the validated requested recipe."""
    return (
        review_codex_output.output_contract_digest()
        if candidate.kind == "native_codex_trial"
        else output_contract_digest()
    )


def candidate_config_digest(candidate: Candidate) -> str:
    """Keep native selections separate from synthetic config-root proof."""
    if candidate.kind == "native_codex_trial":
        return native_fingerprint(candidate)
    return config_fingerprint(
        candidate.config_roots, candidate.environment_sha256, candidate.cli_cwd
    )


def _manifest_origin(manifest: PilotManifest) -> bool:
    """Separate native authority from synthetic provenance before claims."""
    native = manifest.origin == "native_codex_trial"
    if native != (manifest.native_terms is not None):
        raise ValueError("pilot_origin_binding")
    if manifest.native_terms is not None:
        validate_terms(manifest.native_terms, manifest.assessment, time.time())
        if manifest.native_terms.account_path != manifest.account_path:
            raise ValueError("native_account_binding")
    return native


def _candidate_origin(
    candidate: Candidate, manifest: PilotManifest, native: bool
) -> None:
    """Refuse candidate origin or authority mixed across one population."""
    if (
        candidate.kind == "native_codex_trial"
    ) != native or candidate.native_terms != manifest.native_terms:
        raise ValueError("pilot_origin_binding")


def _pilot_prompt(manifest: PilotManifest) -> bytes:
    """Require the frozen bounded prompt and native no-action instruction."""
    policy = manifest.assessment.policies[0]
    native = manifest.origin == "native_codex_trial"
    check_private_path(manifest.learner_prompt_path, directory=False)
    prompt = read_private_file(
        manifest.learner_prompt_path, policy.max_input_bytes
    )
    strict_json(prompt)
    template = PromptArtifact.model_validate_json(prompt)
    if native:
        require_json_instruction(template.instructions)
        require_json_instruction(manifest.task_instructions)
    if hashlib.sha256(prompt).hexdigest() != policy.prompt_digest:
        raise ValueError("pilot_prompt_binding")
    return prompt
