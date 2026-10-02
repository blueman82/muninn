"""Names from before the rename to muninn, kept only where safety needs them.

Old environment variables are ignored, not honoured, but the command line
says so instead of silently changing where data lives.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# Prefix of the old environment variables, and of their replacements.
OLD_ENV_PREFIX = "PCTX_"
NEW_ENV_PREFIX = "MUNINN_"
# Directory name of the old data home, a sibling of the new one.
OLD_DATA_DIR = "provenance-context"


def env_warning(environ: Mapping[str, str]) -> str | None:
    """Say which old environment variables are set and are now ignored.

    Args:
        environ: The process environment.

    Returns:
        One line naming each old variable and its replacement, or None.
    """
    old = sorted(k for k in environ if k.startswith(OLD_ENV_PREFIX))
    if not old:
        return None
    pairs = ", ".join(
        f"{k} (use {NEW_ENV_PREFIX}{k[len(OLD_ENV_PREFIX):]})" for k in old
    )
    return f"muninn: ignoring old environment variables: {pairs}"


def old_data_dir(home: Path) -> Path:
    """Return where an un-migrated old data home would sit beside ``home``."""
    return home.with_name(OLD_DATA_DIR)
