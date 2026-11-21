"""Render pinned provider hooks and turn installed handlers into arguments."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from install import configedit as ce
from install.constants import CLAUDE_EVENTS, CURSOR_EVENTS
from install.context import Ctx
from install.provider_apps import installed
from install.transforms import cursor_handlers, cursor_ours, ours
from install.trust import codex_hooks
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


def cursor_command(ctx: Ctx, event: str = "preCompact") -> str:
    """Return Cursor's event command using the native installed launcher."""
    verb = _VERBS[event]
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
                handler["command"] = (
                    f"{shlex.quote(str(ctx.muninn))} hook {verb}"
                    f" --provider {provider}"
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
    calls = [shlex.split(handler["command"]) for handler in handlers]
    paths: list[Path] = (
        list(ctx.cache.glob("*/hooks/hooks.json"))
        if installed(ctx, "codex")
        else []
    )
    for path in sorted(paths):
        calls.extend(
            shlex.split(hook["command"])
            for hook in codex_hooks(path.read_bytes())
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
    """Return the Codex command line.

    Args:
        ctx: Installation context.
        *arguments: Arguments passed unchanged.

    Returns:
        The program name and exact CLI arguments.
    """
    return ["codex", *arguments]


def codex_identity(ctx: Ctx) -> tuple[str, ...]:
    """Return opaque descriptor-bound identities of the selected CLI files.

    Args:
        ctx: Installation context.

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
