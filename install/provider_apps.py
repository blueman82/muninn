"""Detect installed provider applications independently of saved history."""

from __future__ import annotations

import json
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

from install.context import Ctx

APPLICATIONS = Path("/Applications")
_APP_NAMES = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor"}


def installed(ctx: Ctx, provider: str) -> bool:
    """Check native app bundles on macOS or executable presence elsewhere.

    Args:
        ctx: Installation home and native platform.
        provider: Claude, Codex or Cursor's lowercase provider name.

    Returns:
        Whether the app bundle or provider executable is installed.
    """
    if ctx.platform == "darwin":
        name = f"{_APP_NAMES[provider]}.app"
        return any(
            (root / name).is_dir()
            for root in (APPLICATIONS, ctx.home / "Applications")
        )
    if ctx.platform == "win32":
        return _windows_installed(ctx, provider)
    command = {
        "claude": "claude-desktop",
        "codex": "chatgpt",
        "cursor": "cursor",
    }[provider]
    return shutil.which(command) is not None


def _windows_installed(ctx: Ctx, provider: str) -> bool:
    """Check desktop executable locations and current-user MSIX registration.

    Args:
        ctx: Installation home and runner for read-only package discovery.
        provider: Lowercase provider name.

    Returns:
        Whether a desktop executable or registered MSIX payload exists.
    """
    local = Path(
        os.environ.get("LOCALAPPDATA", str(ctx.home / "AppData/Local"))
        if ctx.default_home
        else ctx.home / "AppData/Local"
    )
    name = _APP_NAMES[provider]
    candidates = [
        local
        / "Programs"
        / ("cursor" if provider == "cursor" else name)
        / f"{name}.exe"
    ]
    if provider == "claude":
        candidates.append(local / "AnthropicClaude/claude.exe")
    candidates.extend(
        Path(base) / name / f"{name}.exe"
        for key in ("ProgramFiles", "ProgramFiles(x86)")
        if (base := os.environ.get(key))
    )
    if any(path.is_file() for path in candidates):
        return True
    if provider == "cursor":
        return False
    package = {"claude": "Claude", "codex": "OpenAI.Codex"}[provider]
    system = Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
    script = (
        f"Get-AppxPackage -Name '{package}' | "
        "Select-Object Name,InstallLocation | ConvertTo-Json -Compress"
    )
    try:
        result = ctx.run(
            [
                system / "System32/WindowsPowerShell/v1.0/powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ]
        )
        if result.returncode or not result.stdout.strip():
            return False
        data: Any = json.loads(result.stdout)
        entries: list[Any] = (
            cast(list[Any], data) if isinstance(data, list) else [data]
        )
        return any(
            isinstance(entry, dict)
            and cast(dict[str, Any], entry).get("Name") == package
            and isinstance(
                cast(dict[str, Any], entry).get("InstallLocation"), str
            )
            and bool(cast(dict[str, Any], entry)["InstallLocation"])
            and _package_payload(
                Path(cast(dict[str, Any], entry)["InstallLocation"])
            )
            for entry in entries
        )
    except (OSError, ValueError):
        return False


def _package_payload(root: Path) -> bool:
    """Require a declared desktop executable inside the registered package.

    Args:
        root: The current user's registered package installation directory.

    Returns:
        Whether its manifest declares an existing executable in that package.
    """
    try:
        manifest = ET.parse(root / "AppxManifest.xml")
        for application in manifest.iter():
            executable = application.get("Executable")
            if (
                application.tag.rsplit("}", 1)[-1] != "Application"
                or not executable
            ):
                continue
            path = (root / executable.replace("\\", "/")).resolve()
            if path.is_relative_to(root.resolve()) and path.is_file():
                return True
    except (OSError, ET.ParseError):
        return False
    return False
