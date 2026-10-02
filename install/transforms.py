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
    """Say whether a settings hook group runs pctx."""
    cmds = " ".join(h.get("command", "") for h in group.get("hooks", []))
    return "/.local/bin/pctx hook" in cmds


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
        groups = cast(list[dict[str, Any]], current)  # hook group objects
        keep = [g for g in groups if not ours(g)]
        ce.jset(obj, ("hooks", event), keep + list(fragment[event]))
    return ce.dump_like(data, obj)


def codex_scan(text: str) -> dict[str, dict[str, Any] | None]:
    """Parse our config.toml sections and refuse stray markers."""
    return ce.scan_named(text, CODEX_KEYS, MARKERS)


def codex_check(before: bytes, after: bytes) -> None:
    """Prove an edit of config.toml touched only our sections.

    Args:
        before: The file bytes before the edit.
        after: The file bytes after the edit.

    A refusal (``configedit.RefusedError``) propagates when the edit touched
    anything else or left a stray marker or unrecognised layout.
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
        The edited text; a refusal (``configedit.RefusedError``) propagates
        for a stray marker or unrecognised layout.
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
        The text without our trust sections; a refusal
        (``configedit.RefusedError``) propagates for a stray marker or
        unrecognised layout.
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
