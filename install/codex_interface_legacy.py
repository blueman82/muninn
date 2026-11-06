"""Read exact predecessor approval without using its removed executable."""

from __future__ import annotations

import hashlib
import platform

from install import legacy_baseline as baseline
from install.context import Ctx, StepFailedError
from muninn import review_codex_launch
from muninn.review_codex_policy import CodexExperiment
from muninn.review_json import (
    canonical_json,
    canonical_json_value,
    result_schema,
)
from muninn.review_launch_inputs import PromptArtifact
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_rounds import round_schema
from muninn.review_runner import (
    RunnerCapability,
    check_private_path,
    read_private_file,
)
from muninn.review_runtime_models import require_legacy_runner


def legacy_facts(
    ctx: Ctx, policy: ReviewPolicy
) -> tuple[review_codex_launch.ExperimentalBinding, str]:
    """Verify saved facts without interpreting a legacy name as hardware."""
    try:
        return _read(ctx, policy)
    except (ValueError, OSError, TypeError, StepFailedError) as exc:
        code = str(exc)
        if code not in {"codex_platform_changed", "codex_data_home_changed"}:
            code = "codex_interface_legacy_approval_invalid"
        raise ValueError(code) from None


def _read(
    ctx: Ctx, policy: ReviewPolicy
) -> tuple[review_codex_launch.ExperimentalBinding, str]:
    """Bind private legacy artifacts and original auth directory identity."""
    require_legacy_runner(policy.runner)
    terms = policy.experimental_codex
    if not isinstance(terms, CodexExperiment):
        raise ValueError("codex_interface_legacy_approval_invalid")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("codex_platform_changed")
    if (
        terms.data_home != ctx.data
        or not ctx.data.is_absolute()
        or ctx.data.resolve() != ctx.data
    ):
        raise ValueError("codex_data_home_changed")
    directory = ctx.data / "review-capabilities"
    for path in (
        ctx.data,
        directory,
        directory / "selected",
        ctx.data / "review-prompts",
    ):
        check_private_path(path, directory=True)
    selected = directory / "selected" / (digest(policy) + ".json")
    selected_raw = read_private_file(selected, 65536)
    capability = RunnerCapability.model_validate_json(selected_raw)
    binding = review_codex_launch.stored_binding(ctx.data, capability)
    prompt = ctx.data / "review-prompts" / (policy.prompt_digest + ".json")
    raw = read_private_file(prompt, policy.max_input_bytes)
    if (
        binding.requested != policy.runner
        or binding.host_identity != terms.host_identity
        or capability.status != "unverified"
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
        or hashlib.sha256(raw).hexdigest() != policy.prompt_digest
        or canonical_json(PromptArtifact.model_validate_json(raw)).encode()
        != raw
    ):
        raise ValueError("codex_interface_legacy_approval_invalid")
    files = (
        (selected, selected_raw),
        (
            capability.proof_path,
            read_private_file(capability.proof_path, 65536),
        ),
        (prompt, raw),
        (binding.schema_path, read_private_file(binding.schema_path, 4096)),
    )
    facts = {
        "files": sorted(
            (str(path), hashlib.sha256(value).hexdigest())
            for path, value in files
        ),
        "auth_home": baseline.directory(binding.auth_home),
        "cli_cwd": baseline.directory(binding.cli_cwd, private=True),
    }
    return (
        binding,
        hashlib.sha256(canonical_json_value(facts).encode()).hexdigest(),
    )
