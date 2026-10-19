"""Construct the single static reviewer recipe without launch authority."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from muninn.review_models import ReviewContext, RunnerConfig
from muninn.review_rounds import round_schema


@dataclass(frozen=True)
class LaunchSpec:
    """Carry fixed launch data without authorizing spending or activation."""

    wrapper_argv: tuple[str, ...]
    wrapper_cwd: Path
    wrapper_env: dict[str, str]
    cli_argv: tuple[str, ...]
    cli_cwd: Path
    cli_env: dict[str, str]
    prompt: bytes
    deadline: float


def cli_argv(
    requested: RunnerConfig, schema: dict[str, object] | None = None
) -> tuple[str, ...]:
    """Fix every CLI option independently of model-controlled data."""
    return (
        requested.executable,
        "-p",
        "--json-schema",
        json.dumps(
            round_schema() if schema is None else schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ),
        "--output-format",
        "json",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--no-session-persistence",
        "--safe-mode",
        "--disable-slash-commands",
        "--model",
        requested.model,
        "--effort",
        requested.reasoning,
    )


def codex_cli_argv(
    requested: RunnerConfig, cwd: Path, schema_path: Path
) -> tuple[str, ...]:
    """Fix the native trial options without arbitrary arguments or fallback."""
    return (
        requested.executable,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--disable",
        "hooks",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "-C",
        str(cwd),
        "--output-schema",
        str(schema_path),
        "--json",
        "--color",
        "never",
        "--model",
        requested.model,
        "-c",
        "model_reasoning_effort=" + requested.reasoning,
        "-",
    )


def launch_spec(
    context: ReviewContext,
    cwd: Path,
    environment: dict[str, str],
    prompt: bytes,
    schema: dict[str, object] | None = None,
    *,
    schema_path: Path | None = None,
) -> LaunchSpec:
    """Build the fixed idle wrapper and explicit isolated CLI environment."""
    if context.policy.runner.runner == "codex_cli" and schema_path is None:
        raise ValueError("native_schema_required")
    argv = (
        codex_cli_argv(context.policy.runner, cwd, schema_path)
        if context.policy.runner.runner == "codex_cli"
        and schema_path is not None
        else cli_argv(context.policy.runner, schema)
    )
    return LaunchSpec(
        (
            sys.executable,
            "-E",
            "-s",
            "-m",
            "muninn.review_worker",
            "--deadline",
            repr(context.deadline),
            "--max-input-bytes",
            str(context.policy.max_input_bytes),
            "--max-output-bytes",
            str(context.policy.max_output_bytes),
        ),
        Path(__file__).resolve().parent.parent,
        {},
        argv,
        cwd,
        environment,
        prompt,
        context.deadline,
    )
