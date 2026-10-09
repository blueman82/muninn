"""Pure edits of Claude's settings.json and Codex's config.toml.

Everything here maps bytes or text to bytes or text and touches no file, so
preflight can dry-apply every edit before anything is changed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from install import configedit as ce
from install.constants import (
    CLAUDE_EVENTS,
    CODEX_KEYS,
    MARKERS,
    MARKETPLACE,
    PLUGIN,
    TRUST,
)
from install.trust import TrustedHook


def ours(group: Mapping[str, Any]) -> bool:
    """Say whether a settings hook group runs muninn."""
    for handler in group.get("hooks", []):
        command = handler.get("command", "")
        if "/.local/bin/muninn" in command and " hook " in command:
            return True
        arguments = handler.get("args", [])
        if (
            any(
                str(argument)
                .replace("\\", "/")
                .casefold()
                .endswith("/muninn/bin/muninn.ps1")
                for argument in arguments
            )
            and "hook" in arguments
        ):
            return True
    return False


def claude_paths(obj: Mapping[str, Any]) -> list[ce.JsonPath]:
    """List the only settings.json keys the installer may change.

    Args:
        obj: The parsed settings; ignored.

    Returns:
        One key path per hook event we manage.
    """
    return [("hooks", e) for e in CLAUDE_EVENTS]


def edit_settings(data: bytes, fragment: Mapping[str, Sequence[Any]]) -> bytes:
    """Replace our hook groups in settings.json with the pinned fragment.

    Args:
        data: The current settings.json bytes.
        fragment: Our hook groups per event, from the pinned release.

    Returns:
        The new bytes; other hook groups and keys are left as they were.

    Raises:
        RefusedError: If an event's hooks value is not a list.
    """
    obj = ce.load_json(data)
    for event in CLAUDE_EVENTS:
        current: Any = ce.jget(obj, ("hooks", event))[1] or []
        if not isinstance(current, list):
            raise ce.RefusedError(f"settings.json hooks.{event} is not a list")
        groups = cast(list[dict[str, Any]], current)
        keep = [g for g in groups if not ours(g)]
        ce.jset(obj, ("hooks", event), keep + list(fragment[event]))
    return ce.dump_like(data, obj)


def cursor_ours(handler: Mapping[str, Any], command: str) -> bool:
    """Say whether a Cursor handler is Muninn's preCompact hook."""
    return handler.get("command") == command


def edit_cursor_settings(data: bytes, command: str, handler: Any) -> bytes:
    """Merge Muninn's preCompact handler while preserving other Cursor hooks.

    Args:
        data: Existing Cursor hooks.json bytes.
        command: Muninn's rendered native hook command.
        handler: The pinned preCompact handler to install.

    Returns:
        The edited JSON, preserving every other hook and top-level value.

    Raises:
        RefusedError: If Cursor's version or hooks shape is unsupported.
    """
    obj = ce.load_json(data)
    if obj.get("version", 1) != 1:
        raise ce.RefusedError("hooks.json version is not supported")
    hooks_value: Any = obj.setdefault("hooks", {})
    if not isinstance(hooks_value, dict):
        raise ce.RefusedError("hooks.json hooks is not an object")
    hooks = cast(dict[str, Any], hooks_value)
    handlers: Any = hooks.get("preCompact", [])
    if not isinstance(handlers, list):
        raise ce.RefusedError("hooks.json hooks.preCompact is not a list")
    handler_list = cast(list[Any], handlers)
    if any(not isinstance(handler, dict) for handler in handler_list):
        raise ce.RefusedError("hooks.json preCompact handler is not an object")
    current = cast(list[dict[str, Any]], handler_list)
    hooks["preCompact"] = [
        h for h in current if not cursor_ours(h, command)
    ] + [handler]
    obj["version"] = 1
    return ce.dump_like(data, obj)


def drop_cursor_settings(data: bytes, command: str) -> bytes:
    """Remove Muninn's Cursor handler and preserve all other hook settings.

    Args:
        data: Cursor hooks.json bytes.
        command: Muninn's rendered native hook command.

    Returns:
        The original bytes when the handler is absent, or edited JSON when
        it is removed.

    Raises:
        RefusedError: If the preCompact event is not a list.
    """
    obj = ce.load_json(data)
    hooks_value: Any = obj.get("hooks", {})
    if not isinstance(hooks_value, dict):
        raise ce.RefusedError("hooks.json hooks is not an object")
    hooks = cast(dict[str, Any], hooks_value)
    handlers: Any = hooks.get("preCompact")
    if handlers is None:
        return data
    if not isinstance(handlers, list):
        raise ce.RefusedError("hooks.json hooks.preCompact is not a list")
    handler_list = cast(list[Any], handlers)
    if any(not isinstance(handler, dict) for handler in handler_list):
        raise ce.RefusedError("hooks.json preCompact handler is not an object")
    current = cast(list[dict[str, Any]], handler_list)
    kept = [h for h in current if not cursor_ours(h, command)]
    if len(kept) == len(current):
        return data
    if kept:
        hooks["preCompact"] = kept
    else:
        hooks.pop("preCompact")
    if not hooks:
        obj.pop("hooks")
    return ce.dump_like(data, obj)


def restore_cursor_settings(
    data: bytes,
    command: str,
    owned: Sequence[Mapping[str, Any]],
    version: Mapping[str, Any],
) -> bytes:
    """Restore prior owned handlers while retaining current foreign config.

    Args:
        data: Current Cursor config bytes.
        command: Exact native command owned by this installation.
        owned: Prior owned handlers and their original list positions.
        version: Before-value of the installer's version key.

    Returns:
        Config with only owned entries restored, preserving foreign changes.

    Raises:
        RefusedError: If current hook containers have malformed shapes.
    """
    obj = ce.load_json(drop_cursor_settings(data, command))
    hooks = cast(dict[str, Any], obj.setdefault("hooks", {}))
    handlers = cast(list[Any], hooks.get("preCompact", []))
    for entry in owned:
        handlers.insert(min(entry["index"], len(handlers)), entry["value"])
    if handlers:
        hooks["preCompact"] = handlers
    if not hooks:
        obj.pop("hooks")
    if type(obj.get("version")) is int and obj["version"] == 1:
        if version["present"]:
            obj["version"] = version["value"]
        elif not obj.get("hooks"):
            obj.pop("version")
    return ce.dump_like(data, obj)


def codex_scan(text: str) -> dict[str, dict[str, Any] | None]:
    """Parse our config.toml sections and refuse stray markers.

    Args:
        text: The whole config.toml.

    Returns:
        Each of our sections parsed, or None when it is absent.

    Raises:
        RefusedError: For a stray marker or unrecognised layout.
    """
    return ce.scan_named(text, CODEX_KEYS, MARKERS)


def codex_check(before: bytes, after: bytes) -> None:
    """Prove an edit of config.toml touched only our sections.

    Args:
        before: The file bytes before the edit.
        after: The file bytes after the edit.

    Raises:
        RefusedError: If the edit touched anything else or left a stray
            marker or unrecognised layout.
    """
    ce.toml_check(before.decode(), after.decode(), CODEX_KEYS, MARKERS)
    codex_scan(after.decode())


def set_line(text: str, header: str, key: str, value: str) -> str:
    """Set one ``key = value`` line in a named section, adding it if absent.

    Args:
        text: The whole config.toml.
        header: Header of one of our sections.
        key: Key to set.
        value: TOML literal to store.

    Returns:
        The edited text.

    Raises:
        RefusedError: For a stray marker or unrecognised layout.
    """
    codex_scan(text)
    raw, line = ce.get_section(text, header), f"{key} = {value}\n"
    if raw is None:
        return ce.put_section(text, header, f"\n{header}\n{line}")
    lines = raw.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if re.match(rf"\s*{key}\s*=", ln)]
    if hits:
        lines[hits[0]] = line
    else:
        head = next(i for i, ln in enumerate(lines) if ln.strip() == header)
        lines.insert(head + 1, line)
    return ce.put_section(text, header, "".join(lines))


def drop_trust(text: str) -> str:
    """Remove every hook trust section we manage.

    Args:
        text: The whole config.toml.

    Returns:
        The text without our trust sections.

    Raises:
        RefusedError: For a stray marker or unrecognised layout.
    """
    codex_scan(text)
    for header in TRUST.values():
        text = ce.put_section(text, header, None)
    return text


def repoint(text: str, source: str) -> str:
    """Point our local marketplace at ``source``.

    Args:
        text: The whole config.toml.
        source: Directory of the pinned Codex integration.

    Returns:
        The edited text.

    Raises:
        RefusedError: If the marketplace exists but is not a local one.
    """
    mkt = codex_scan(text)[MARKETPLACE]
    if mkt is None:
        text = set_line(text, MARKETPLACE, "source_type", '"local"')
    elif mkt.get("source_type") != "local":
        raise ce.RefusedError(f"{MARKETPLACE}: source_type is not local")
    return set_line(text, MARKETPLACE, "source", json.dumps(source))


def enable(text: str) -> str:
    """Enable our plugin."""
    return set_line(text, PLUGIN, "enabled", "true")


def write_trust(text: str, hooks: Sequence[TrustedHook]) -> str:
    """Write the trust hash of each hook.

    Args:
        text: The whole config.toml.
        hooks: Hooks with their Codex trust hashes.

    Returns:
        The edited text.

    Raises:
        RefusedError: If a hook is not the first handler of the first group,
            the only position the trust keys cover.
    """
    codex_scan(text)
    for hook in hooks:
        if not hook["suffix"].endswith(":0:0"):
            raise ce.RefusedError(f"no trust key for hook {hook['suffix']}")
        header = TRUST[hook["event"]]
        block = f'\n{header}\ntrusted_hash = "{hook["hash"]}"\n'
        text = ce.put_section(text, header, block)
    return text
