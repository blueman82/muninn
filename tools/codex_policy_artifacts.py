"""Construct ongoing Codex artifacts without reading or publishing files."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from muninn.review_codex_launch import (
    JSON_ONLY_INSTRUCTION,
    ExperimentalBinding,
    wire_schema,
)
from muninn.review_codex_output import output_contract_digest
from muninn.review_json import (
    canonical_json,
    canonical_json_value,
    result_schema,
)
from muninn.review_launch_inputs import PromptArtifact
from muninn.review_models import ReviewPolicy
from muninn.review_rounds import round_schema
from muninn.review_runner import RunnerCapability

_OUTPUT_CONTRACT_DIGEST = output_contract_digest()


@dataclass(frozen=True)
class PreparedFile:
    """Retain exact bytes confined to the fixed review-artifact namespace."""

    relative_path: Path
    raw: bytes

    def __post_init__(self) -> None:
        """Refuse arbitrary publication paths even in manually built plans."""
        path = self.relative_path.as_posix()
        if type(self.raw) is not bytes or not re.fullmatch(
            r"review-codex-schema\.json|review-prompts/[0-9a-f]{64}\.json|"
            r"review-capabilities/(selected/)?[0-9a-f]{64}\.json",
            path,
        ):
            raise ValueError("preparation_artifact_invalid")


@dataclass(frozen=True)
class ArtifactPlan:
    """Bind sorted relative artifact paths and their complete exact bytes."""

    files: tuple[PreparedFile, ...]
    policy_digest: str
    digest: str


def _digest(raw: bytes) -> str:
    """Compute the content address used by installed artifact readers."""
    return hashlib.sha256(raw).hexdigest()


def plan_artifacts(
    policy: ReviewPolicy,
    prompt: bytes,
    binding: ExperimentalBinding,
    config_digest: str,
) -> ArtifactPlan:
    """Build the existing unverified recipe from checked explicit inputs.

    Args:
        policy: Exact candidate policy, including its original approval.
        prompt: Exact retained trusted prompt bytes.
        binding: Checked runner, host and fixed absolute artifact paths.
        config_digest: Fingerprint computed by the caller outside a writer.

    Returns:
        Four immutable review artifacts and their complete recipe digest.

    Raises:
        ValueError: If explicit inputs do not match the retained recipe.
    """
    terms = policy.experimental_codex
    home = binding.schema_path.parent
    parsed = PromptArtifact.model_validate_json(prompt)
    if (
        terms is None
        or not home.is_absolute()
        or home != terms.data_home
        or binding.schema_path != home / "review-codex-schema.json"
        or binding.cli_cwd != home / "review-runner-cwd"
        or ".." in home.parts
        or not binding.auth_home.is_absolute()
        or ".." in binding.auth_home.parts
        or binding.auth_home == home
        or home in binding.auth_home.parents
        or not Path(policy.runner.executable).is_absolute()
        or binding.requested != policy.runner
        or binding.host_identity != terms.host_identity
        or binding.output_contract_digest != _OUTPUT_CONTRACT_DIGEST
        or _digest(prompt) != policy.prompt_digest
        or canonical_json(parsed).encode() != prompt
        or JSON_ONLY_INSTRUCTION not in parsed.instructions
        or policy.result_schema_digest
        != _digest(canonical_json_value(result_schema()).encode())
        or not re.fullmatch(r"[0-9a-f]{64}", config_digest)
    ):
        raise ValueError("preparation_artifact_invalid")
    binding_raw = canonical_json(binding).encode()
    proof = Path("review-capabilities") / (_digest(binding_raw) + ".json")
    policy_digest = _digest(canonical_json(policy).encode())
    capability = RunnerCapability(
        **policy.runner.model_dump(),
        status="unverified",
        proof_path=home / proof,
        schema_digest=_digest(canonical_json_value(round_schema()).encode()),
        config_digest=config_digest,
    )
    files = tuple(
        sorted(
            (
                PreparedFile(Path("review-codex-schema.json"), wire_schema()),
                PreparedFile(
                    Path("review-prompts") / (policy.prompt_digest + ".json"),
                    prompt,
                ),
                PreparedFile(proof, binding_raw),
                PreparedFile(
                    Path("review-capabilities/selected")
                    / (policy_digest + ".json"),
                    canonical_json(capability).encode(),
                ),
            ),
            key=lambda item: item.relative_path.as_posix(),
        )
    )
    digest = hashlib.sha256()
    for item in files:
        for raw in (item.relative_path.as_posix().encode(), item.raw):
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
    return ArtifactPlan(files, policy_digest, digest.hexdigest())
