"""Build and decode one Claude CLI call."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import cast

from muninn.review_json import canonical_json_value, load_object
from muninn.review_settings import Settings

NAME = "claude"


def find_executable() -> str | None:
    """Find claude on PATH, then in the usual user install places."""
    found = shutil.which("claude")
    if found:
        return found
    for path in (
        Path.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def argv(
    settings: Settings, schema_json: str, executable: str, workdir: Path
) -> tuple[str, ...]:
    """Fix every option so the call has no tools, settings or session."""
    return (
        executable,
        "-p",
        "--json-schema",
        schema_json,
        "--output-format",
        "json",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--safe-mode",
        "--disable-slash-commands",
        "--model",
        settings.model,
        "--effort",
        settings.effort,
    )


def decode(stdout: bytes, limit: int) -> bytes:
    """Return the canonical structured result of a successful call.

    Raises:
        ValueError: With output_limit or invalid_native_output.
    """
    if len(stdout) > limit:
        raise ValueError("output_limit")
    try:
        envelope = load_object(stdout)
        if (
            envelope.get("type") != "result"
            or envelope.get("subtype") != "success"
            or envelope.get("is_error") is not False
        ):
            raise ValueError("invalid_native_output")
        result = envelope.get("structured_output")
        if not isinstance(result, dict):
            raise ValueError("invalid_native_output")
        body = canonical_json_value(cast(dict[str, object], result)).encode()
    except (ValueError, RecursionError, UnicodeError):
        raise ValueError("invalid_native_output") from None
    if len(body) > limit:
        raise ValueError("output_limit")
    return body
