"""Prepare only the explicitly approved ongoing Mac Codex production policy."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
from typing import cast

from muninn import review_runner, store
from muninn.knowledge_models import Digest, StrictModel
from muninn.review_codex_output import output_contract_digest
from muninn.review_codex_policy import (
    PINNED_EXECUTABLE,
    host_identity,
)
from muninn.review_json import canonical_json_value, result_schema
from muninn.review_launch_spec import LaunchSpec, codex_cli_argv, launch_spec
from muninn.review_models import ReviewContext, RunnerConfig
from muninn.review_rounds import round_schema

JSON_ONLY_INSTRUCTION = (
    "Return only the required JSON object. Do not invoke tools, commands, "
    "MCP, web search, or file operations."
)
WIRE_INSTRUCTION = (
    "Serialize the JSON object matching payload_schema into the payload_json "
    "string of the required outer object. "
    "Do not return the inner object directly."
)


class ExperimentalBinding(StrictModel):
    """Bind selected inputs without claiming complete isolation."""

    requested: RunnerConfig
    host_identity: Digest
    executable_sha256: Digest
    output_contract_digest: Digest
    cli_cwd: Path
    auth_home: Path
    schema_path: Path


def wire_schema() -> bytes:
    """Use the same strict object envelope as the measured native trial."""
    return canonical_json_value(
        {
            "type": "object",
            "properties": {"payload_json": {"type": "string"}},
            "required": ["payload_json"],
            "additionalProperties": False,
        }
    ).encode()


def fingerprint(binding: ExperimentalBinding) -> str:
    """Bind exact selected inputs, decoder, argv and disposable cwd state."""
    return hashlib.sha256(
        canonical_json_value(
            {
                "binding": binding.model_dump(mode="json"),
                "argv": codex_cli_argv(
                    binding.requested, binding.cli_cwd, binding.schema_path
                ),
                "cwd": review_runner.config_fingerprint(
                    (binding.cli_cwd,), {}, binding.cli_cwd
                ),
                "wire_schema": wire_schema().decode(),
                "semantic_schema": round_schema(),
                "json_instruction": JSON_ONLY_INSTRUCTION,
                "wire_instruction": WIRE_INSTRUCTION,
            }
        ).encode()
    ).hexdigest()


def _binding(
    home: Path, capability: review_runner.RunnerCapability
) -> ExperimentalBinding:
    """Load a content-addressed private explicit experimental selection."""
    path = capability.proof_path
    if path.parent != home / "review-capabilities" or path.suffix != ".json":
        raise ValueError("capability_storage")
    review_runner.check_private_path(path.parent, directory=True)
    raw = review_runner.read_private_file(path, 65536)
    if hashlib.sha256(raw).hexdigest() != path.stem:
        raise ValueError("capability_changed")
    binding = ExperimentalBinding.model_validate_json(raw)
    if (
        binding.cli_cwd != home / "review-runner-cwd"
        or binding.schema_path != home / "review-codex-schema.json"
        or binding.auth_home.resolve() != binding.auth_home
        or not binding.auth_home.is_absolute()
        or not binding.auth_home.is_dir()
        or binding.auth_home == home
        or home in binding.auth_home.parents
        or binding.executable_sha256 != PINNED_EXECUTABLE
        or binding.output_contract_digest != output_contract_digest()
        or fingerprint(binding) != capability.config_digest
    ):
        raise ValueError("capability_changed")
    review_runner.check_private_path(binding.cli_cwd, directory=True)
    if tuple(binding.cli_cwd.iterdir()):
        raise ValueError("capability_changed")
    if (
        review_runner.read_private_file(binding.schema_path, 4096)
        != wire_schema()
    ):
        raise ValueError("capability_changed")
    executable = Path(binding.requested.executable)
    if review_runner.file_digest(executable, 256 * 1024 * 1024)[
        0
    ] != PINNED_EXECUTABLE or not os.access(executable, os.X_OK):
        raise ValueError("capability_changed")
    return binding


def prepare(
    context: ReviewContext,
    capability: review_runner.RunnerCapability,
    prompt: bytes,
) -> LaunchSpec:
    """Recheck scope, host and exact recipe immediately before use."""
    policy = context.policy
    terms = policy.experimental_codex
    home = store.data_home(os.environ)
    review_runner.check_private_path(home, directory=True)
    if (
        terms is None
        or terms.data_home != home
        or capability.status != "unverified"
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or terms.host_identity != host_identity()
        or context.scope.project_id is None
        or not (home / "recall.off").is_file()
    ):
        raise ValueError("codex_experimental_boundary")
    binding = _binding(home, capability)
    if (
        binding.requested != policy.runner
        or binding.host_identity != terms.host_identity
        or any(
            getattr(capability, key) != value
            for key, value in policy.runner.model_dump().items()
        )
        or capability.schema_digest
        != hashlib.sha256(
            canonical_json_value(round_schema()).encode()
        ).hexdigest()
        or policy.result_schema_digest
        != hashlib.sha256(
            canonical_json_value(result_schema()).encode()
        ).hexdigest()
    ):
        raise ValueError("capability_changed")
    data = cast(dict[str, object], json.loads(prompt))
    instructions = data.get("instructions")
    if (
        not isinstance(instructions, str)
        or JSON_ONLY_INSTRUCTION not in instructions
    ):
        raise ValueError("native_prompt_instruction")
    if "payload_schema" in data:
        raise ValueError("native_prompt_instruction")
    data["instructions"] = instructions + " " + WIRE_INSTRUCTION
    data["payload_schema"] = round_schema()
    native = canonical_json_value(data).encode()
    if len(native) > policy.max_input_bytes:
        raise ValueError("invalid_prompt_size")
    return launch_spec(
        context,
        binding.cli_cwd,
        {
            "HOME": str(home),
            "CODEX_HOME": str(binding.auth_home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        },
        native,
        schema_path=binding.schema_path,
    )
