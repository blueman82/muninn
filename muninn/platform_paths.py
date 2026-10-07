"""Native Windows application paths and private release selection."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from muninn import platform_io

_SELECTION_LIMIT = 4096
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def windows_base(env: Mapping[str, str], *, home: Path | None = None) -> Path:
    """Locate Windows application state with safe explicit-home semantics.

    Args:
        env: Environment used only when no explicit home was supplied.
        home: Explicit user home, including isolated installer test homes.

    Returns:
        The application base containing data, lib and bin directories.
    """
    if home is not None:
        return home / "AppData" / "Local" / "Muninn"
    local = env.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Muninn"
    profile = env.get("USERPROFILE") or env.get("HOME")
    user = Path(profile) if profile else Path.home()
    return user / "AppData" / "Local" / "Muninn"


def read_selection(base: Path) -> tuple[Path, Path] | None:
    """Validate an exact private installed-release manifest.

    Args:
        base: Native application base containing lib/selection.json.

    Returns:
        Selected confined release and recorded interpreter, or None when
        no manifest exists. Interpreter links may resolve to their executable.

    Raises:
        ValueError: If a present manifest, release or interpreter is unsafe.
    """
    lib, manifest = base / "lib", base / "lib" / "selection.json"
    if not manifest.exists():
        return None
    try:
        for directory in (base, lib):
            if not platform_io.is_private(directory, directory=True):
                raise ValueError("unsafe installed release directory")
        if not platform_io.is_private(manifest):
            raise ValueError("unsafe release selection manifest")
        with platform_io.open_regular(manifest, root=lib) as handle:
            raw = handle.read(_SELECTION_LIMIT + 1)
        if len(raw) > _SELECTION_LIMIT:
            raise ValueError("release selection manifest is too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("release selection must be an object")
        record = cast("dict[str, object]", value)
        sha, python = _fields(record)
        release, interpreter = lib / sha, Path(python)
        if not platform_io.is_private(release, directory=True):
            raise ValueError("unsafe or absent selected release")
        if not interpreter.is_absolute() or not interpreter.is_file():
            raise ValueError("recorded interpreter is absent or relative")
        checked = (
            interpreter if sys.platform == "win32" else interpreter.resolve()
        )
        with platform_io.open_regular(checked):
            pass
        return release, interpreter
    except (OSError, RecursionError) as exc:
        raise ValueError("release selection cannot be validated") from exc


def _fields(record: dict[str, object]) -> tuple[str, str]:
    """Read only the exact pinned release and interpreter fields."""
    sha, python = record.get("sha"), record.get("python")
    if (
        set(record) != {"sha", "python"}
        or not isinstance(sha, str)
        or _SHA.fullmatch(sha) is None
        or not isinstance(python, str)
    ):
        raise ValueError("invalid release selection fields")
    return sha, python
