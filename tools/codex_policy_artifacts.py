"""Construct ongoing Codex artifacts without reading or publishing files."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from muninn import file_sync, review_runner
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
    return ArtifactPlan(files, policy_digest, recipe_digest(files))


def recipe_digest(files: tuple[PreparedFile, ...]) -> str:
    """Bind every ordered path and byte using the existing length framing."""
    digest = hashlib.sha256()
    for item in files:
        for raw in (item.relative_path.as_posix().encode(), item.raw):
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
    return digest.hexdigest()


def validate_artifacts(home: Path, plan: ArtifactPlan) -> None:
    """Check the complete recipe before publication."""
    if len(plan.files) != 4 or recipe_digest(plan.files) != plan.digest:
        raise ValueError("preparation_artifact_invalid")
    paths = [item.relative_path.as_posix() for item in plan.files]
    if paths != sorted(set(paths)) or not re.fullmatch(
        r"[0-9a-f]{64}", plan.policy_digest
    ):
        raise ValueError("preparation_artifact_invalid")
    items = {item.relative_path.as_posix(): item.raw for item in plan.files}
    selected = "review-capabilities/selected/" + plan.policy_digest + ".json"
    if (
        items.get("review-codex-schema.json") != wire_schema()
        or selected not in items
    ):
        raise ValueError("preparation_artifact_invalid")
    capability = RunnerCapability.model_validate_json(items[selected])
    proof = capability.proof_path
    if (
        proof.parent != home / "review-capabilities"
        or capability.status != "unverified"
    ):
        raise ValueError("preparation_artifact_invalid")
    binding_raw = items.get("review-capabilities/" + proof.name)
    if binding_raw is None or _digest(binding_raw) != proof.stem:
        raise ValueError("preparation_artifact_invalid")
    binding = ExperimentalBinding.model_validate_json(binding_raw)
    if binding.schema_path != home / "review-codex-schema.json":
        raise ValueError("preparation_artifact_invalid")
    prompts = [
        (name, raw)
        for name, raw in items.items()
        if name.startswith("review-prompts/")
    ]
    if (
        len(prompts) != 1
        or prompts[0][0]
        != "review-prompts/" + _digest(prompts[0][1]) + ".json"
    ):
        raise ValueError("preparation_artifact_invalid")
    for item in plan.files:
        PreparedFile(item.relative_path, item.raw)


def publish_artifacts(home: Path, plan: ArtifactPlan) -> None:
    """Publish private artifacts or require exact existing bytes."""
    review_runner.check_private_path(home, directory=True)
    validate_artifacts(home, plan)
    for item in plan.files:
        PreparedFile(item.relative_path, item.raw)
        path = home / item.relative_path
        for parent in reversed(path.relative_to(home).parents):
            directory = home / parent
            directory.mkdir(mode=0o700, exist_ok=True)
            review_runner.check_private_path(directory, directory=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if review_runner.read_private_file(path, 1024 * 1024) != item.raw:
                raise ValueError("preparation_artifact_changed") from None
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(item.raw)
            handle.flush()
            file_sync.sync_fd(handle.fileno())
        file_sync.sync_path(path.parent)
