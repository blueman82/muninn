"""Build and decode one Codex CLI call; off unless selected in settings."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from muninn.review_json import canonical_json_value, load_object
from muninn.review_settings import Settings

NAME = "codex"
_TOOL_ITEMS = {
    "command_execution",
    "mcp_tool_call",
    "web_search",
    "file_change",
}


def find_executable() -> str | None:
    """Find codex on PATH."""
    return shutil.which("codex")


def argv(
    settings: Settings, schema_json: str, executable: str, workdir: Path
) -> tuple[str, ...]:
    """Fix every option; the caller writes schema_json to schema.json.

    Args:
        settings: Learner settings.
        schema_json: Result schema, written by the caller to schema.json.
        executable: Path of the codex program.
        workdir: Empty directory that also holds schema.json.

    Returns:
        The full command line.
    """
    return (
        executable,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "-C",
        str(workdir),
        "--output-schema",
        str(workdir / "schema.json"),
        "--json",
        "--model",
        settings.model,
        "-c",
        "model_reasoning_effort=" + settings.effort,
        "-",
    )


def decode(stdout: bytes, limit: int) -> bytes:
    """Return the canonical final message of a call without tool use.

    Raises:
        ValueError: With output_limit or invalid_native_output.
    """
    if len(stdout) > limit:
        raise ValueError("output_limit")
    final: str | None = None
    try:
        for line in stdout.decode("utf-8").splitlines():
            item = load_object(line).get("item")
            if not isinstance(item, dict):
                continue
            fields = cast(dict[str, object], item)
            if fields.get("type") in _TOOL_ITEMS:
                raise ValueError("invalid_native_output")
            if fields.get("type") == "agent_message":
                text = fields.get("text")
                if not isinstance(text, str):
                    raise ValueError("invalid_native_output")
                final = text
        if final is None:
            raise ValueError("invalid_native_output")
        body = canonical_json_value(load_object(final)).encode()
    except (ValueError, RecursionError, UnicodeError):
        raise ValueError("invalid_native_output") from None
    if len(body) > limit:
        raise ValueError("output_limit")
    return body
