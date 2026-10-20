"""Render pinned provider hooks and turn installed handlers into arguments."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from install import configedit as ce
from install.constants import CLAUDE_EVENTS, CURSOR_EVENTS
from install.context import Ctx
from install.provider_apps import installed
from install.transforms import cursor_handlers, cursor_ours, ours
from install.trust import codex_hooks
from muninn import platform_windows
from muninn.platform_io import open_regular

_VERBS = {
    "SessionStart": "session-start",
    "SessionEnd": "session-end",
    "PreCompact": "pre-compact",
    "sessionStart": "session-start",
    "sessionEnd": "session-end",
    "preCompact": "pre-compact",
    "UserPromptSubmit": "prompt",
}


def powershell() -> str:
    """Return the native Windows PowerShell executable's absolute path."""
    return str(
        Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )


def _windows_command(ctx: Ctx, verb: str, provider: str) -> str:
    """Encode the script path so cmd cannot expand path metacharacters."""
    script = str(ctx.muninn.with_suffix(".ps1")).replace("'", "''")
    statement = (
        f"& '{script}' hook {verb} --provider {provider}; exit $LASTEXITCODE"
    )
    encoded = base64.b64encode(statement.encode("utf-16-le")).decode("ascii")
    return (
        f'"{powershell()}" -NoProfile -NonInteractive '
        f"-ExecutionPolicy Bypass -EncodedCommand {encoded}"
    )


def cursor_command(ctx: Ctx, event: str = "preCompact") -> str:
    """Return Cursor's event command using the native installed launcher."""
    verb = _VERBS[event]
    if ctx.platform == "win32":
        return _windows_command(ctx, verb, "cursor")
    return f"{shlex.quote(str(ctx.muninn))} hook {verb} --provider cursor"


def cursor_commands(ctx: Ctx) -> dict[str, str]:
    """Return the full set of event commands owned by this installation."""
    return {event: cursor_command(ctx, event) for event in CURSOR_EVENTS}


def render_pinned(ctx: Ctx, relative: str, data: bytes) -> bytes:
    """Render a pinned template without substituting raw bytes into JSON.

    Args:
        ctx: Explicit installation home and platform.
        relative: Repository-relative template path.
        data: Original committed template bytes.

    Returns:
        Serialized hooks with escaped native paths, or the substituted text.
    """
    if relative not in (
        "integrations/claude/settings-hooks.json",
        "integrations/codex/hooks/hooks.json",
        "integrations/cursor/hooks.json",
    ):
        return data.replace(b"@HOME@", str(ctx.home).encode())
    doc: dict[str, Any] = json.loads(data)
    if "/cursor/" in relative:
        for event, handlers in doc["hooks"].items():
            for handler in handlers:
                handler["command"] = cursor_command(ctx, event)
        return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()
    provider = "claude" if "/claude/" in relative else "codex"
    for event, groups in doc["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                verb = _VERBS[event]
                if ctx.platform != "win32":
                    handler["command"] = (
                        f"{shlex.quote(str(ctx.muninn))} hook {verb}"
                        f" --provider {provider}"
                    )
                elif provider == "claude":
                    handler["command"] = powershell()
                    handler["args"] = [
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(ctx.muninn.with_suffix(".ps1")),
                        "hook",
                        verb,
                        "--provider",
                        provider,
                    ]
                else:
                    handler["command_windows"] = _windows_command(
                        ctx, verb, provider
                    )
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()


def hook_invocations(ctx: Ctx) -> list[list[str]]:
    """List native executable argument vectors for the installed hooks.

    Args:
        ctx: Installation context with provider settings and plugin cache.

    Returns:
        Argument vectors ready for ctx.run without a POSIX-only tokenizer.
    """
    settings: dict[str, Any] = (
        ce.load_json(ce.read_file(ctx.settings))
        if installed(ctx, "claude") and ctx.settings.exists()
        else {}
    )
    handlers = [
        handler
        for event in CLAUDE_EVENTS
        for group in settings.get("hooks", {}).get(event, [])
        if ours(group)
        for handler in group["hooks"]
    ]
    calls = [
        (
            list(map(str, [handler["command"], *handler["args"]]))
            if "args" in handler
            else shlex.split(handler["command"])
        )
        for handler in handlers
    ]
    paths: list[Path] = (
        list(ctx.cache.glob("*/hooks/hooks.json"))
        if installed(ctx, "codex")
        else []
    )
    for path in sorted(paths):
        calls.extend(
            shlex.split(hook["command"])
            for hook in codex_hooks(path.read_bytes(), platform=ctx.platform)
        )
    if (
        ctx.cursor_hooks
        and installed(ctx, "cursor")
        and ctx.cursor_settings.exists()
    ):
        doc = ce.load_json(ce.read_file(ctx.cursor_settings))
        calls.extend(
            shlex.split(command)
            for event, command in cursor_commands(ctx).items()
            for handler in cursor_handlers(doc, event)
            if cursor_ours(handler, command)
        )
    return calls


def codex_argv(ctx: Ctx, *arguments: str) -> list[str]:
    """Resolve a real Windows executable or the official npm Node entry.

    Args:
        ctx: Installation platform; POSIX keeps its normal CLI dispatch.
        *arguments: Arguments passed unchanged without a cmd shell.

    Returns:
        Real executable and exact CLI arguments.

    Raises:
        OSError: If an existing candidate is unsafe or dispatch is unsupported.
    """
    if ctx.platform != "win32":
        return ["codex", *arguments]
    native = shutil.which("codex.exe")
    if native:
        _windows_executable(Path(native))
        return [native, *arguments]
    shim = shutil.which("codex")
    if not shim:
        raise OSError("Codex executable is unavailable")
    _windows_executable(Path(shim))
    script = Path(shim).parent / "node_modules/@openai/codex/bin/codex.js"
    node = shutil.which("node.exe")
    if not script.is_file() or not node:
        raise OSError(
            "put native codex.exe on PATH or install the official npm CLI"
        )
    _windows_executable(script)
    _windows_executable(Path(node))
    return [node, str(script), *arguments]


def _windows_executable(path: Path) -> None:
    """Validate executable ancestry using actual native Windows semantics."""
    if sys.platform != "win32":
        raise OSError("Windows executable validation requires Windows")
    platform_windows.assert_executable(path)


def codex_identity(ctx: Ctx) -> tuple[str, ...]:
    """Return opaque descriptor-bound identities of the selected CLI files.

    Args:
        ctx: Native executable or official npm dispatch selection.

    Returns:
        Content, filesystem identity and resolved path hashes per CLI file.

    Raises:
        OSError: If a chosen file is unsafe, missing or changed identity.
    """
    identities: list[str] = []
    for argument in codex_argv(ctx):
        candidate = Path(shutil.which(argument) or argument).resolve(
            strict=True
        )
        with open_regular(candidate, expected=candidate.lstat()) as source:
            status = os.fstat(source.fileno())
            metadata = (
                status.st_dev,
                status.st_ino,
                status.st_size,
                status.st_mtime_ns,
            )
            digest = hashlib.sha256(os.fsencode(candidate))
            digest.update(str(metadata).encode())
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
            identities.append(digest.hexdigest())
    return tuple(identities)
