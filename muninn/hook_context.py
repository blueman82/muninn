"""Provider hook payload and project context helpers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

__all__ = ["as_payload", "env_for_provider", "project_cwd"]


def as_payload(raw: object) -> Mapping[str, object]:
    """Treat anything that is not a JSON object as an empty payload."""
    return (
        cast("Mapping[str, object]", raw) if isinstance(raw, Mapping) else {}
    )


def env_for_provider(
    env: Mapping[str, str], provider: str
) -> Mapping[str, str]:
    """Ignore Cursor's project variable when another provider runs a hook.

    Args:
        env: The inherited process environment.
        provider: Provider that invoked the hook.

    Returns:
        The original environment or a copy without Cursor's project override.
    """
    if provider == "cursor" or "CURSOR_PROJECT_DIR" not in env:
        return env
    return {**env, "CURSOR_PROJECT_DIR": ""}


def project_cwd(payload: Mapping[str, object], env: Mapping[str, str]) -> str:
    """Return Cursor's project directory, payload cwd, process cwd, or empty.

    Args:
        payload: The hook payload.
        env: Environment already scoped for its provider.

    Returns:
        The selected working directory, or an empty string if unavailable.
    """
    if project_dir := env.get("CURSOR_PROJECT_DIR"):
        return project_dir
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    try:
        return str(Path.cwd())
    except OSError:  # the directory was deleted under us
        return ""
