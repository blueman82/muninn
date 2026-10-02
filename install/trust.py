"""The Codex hook trust hash, computed the way Codex computes it.

Codex trusts a hook by the hash of its normalised identity. To pre-trust
our hooks we must reproduce that hash exactly; a hash that differs by one
byte makes Codex report the hook as modified. The test suite compares the
result with a real Codex ``hooks/list`` answer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, TypedDict

from install.context import StepFailedError

# Codex's internal event names, keyed by the names used in hooks.json.
LABELS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use",
}
# Codex fills in this timeout when a hook declares none.
DEFAULT_TIMEOUT_S = 600
# Codex omits additionalContextLimit from the hashed identity at its default.
DEFAULT_CONTEXT_LIMIT = 2500


class TrustedHook(TypedDict):
    """A hook with the key suffix and hash Codex will derive for it.

    Attributes:
        event: Codex's event name, such as ``session_start``.
        suffix: ``<event>:<group>:<handler>`` part of Codex's trust key.
        command: The shell command the hook runs.
        hash: ``sha256:`` digest of the normalised identity.
    """

    event: str
    suffix: str
    command: str
    hash: str


def _handler(hook: dict[str, Any]) -> dict[str, Any]:
    """Normalise one hooks.json handler as Codex does before hashing.

    Args:
        hook: A command hook object from hooks.json.

    Returns:
        The handler with defaults filled in and unset fields dropped.
    """
    handler: dict[str, Any] = {
        "type": "command",
        "command": hook["command"],
        "timeout": max(hook.get("timeout", DEFAULT_TIMEOUT_S), 1),
        "async": hook.get("async", False),
    }
    if hook.get("statusMessage") is not None:
        handler["statusMessage"] = hook["statusMessage"]
    limit = hook.get("additionalContextLimit")
    if limit not in (None, DEFAULT_CONTEXT_LIMIT):
        handler["additionalContextLimit"] = limit
    return handler


def _digest(event: str, group: dict[str, Any], hook: dict[str, Any]) -> str:
    """Hash one hook's identity.

    Args:
        event: The event name as written in hooks.json.
        group: The matcher group holding the hook.
        hook: The command hook object.

    Returns:
        ``sha256:`` followed by the hex digest.
    """
    ident: dict[str, Any] = {
        "event_name": LABELS[event],
        "hooks": [_handler(hook)],
    }
    matcher = group.get("matcher")
    # Codex ignores a matcher on UserPromptSubmit, so it is not hashed.
    if event != "UserPromptSubmit" and matcher is not None:
        ident["matcher"] = matcher
    # Key-sorted compact JSON, unescaped unicode: the exact bytes Codex
    # hashes. Any other serialisation yields a different digest.
    blob = json.dumps(
        ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return f"sha256:{hashlib.sha256(blob.encode()).hexdigest()}"


def codex_hooks(data: bytes) -> list[TrustedHook]:
    """List each command hook in a hooks.json with its Codex trust hash.

    This ports Codex rust-v0.159.2: handler normalisation in
    hooks/src/engine/discovery.rs (lines 505-566, timeout default at 762),
    hook_hash (769-792), the sha256 of key-sorted compact JSON in
    config/src/fingerprint.rs (54-84), None fields dropped as in toml 0.9.11
    table.rs (385-401), and UserPromptSubmit carrying no matcher
    (hooks/src/events/common.rs 112-128).

    Args:
        data: The bytes of a hooks.json file.

    Returns:
        One entry per hook, in file order.

    Raises:
        StepFailedError: If a hook is not a command hook or its event is not
            one Codex supports.
    """
    out: list[TrustedHook] = []
    for event, groups in json.loads(data)["hooks"].items():
        for gi, group in enumerate(groups):
            for hi, h in enumerate(group["hooks"]):
                if h.get("type") != "command" or event not in LABELS:
                    raise StepFailedError(f"unsupported Codex hook in {event}")
                out.append(
                    {
                        "event": LABELS[event],
                        "suffix": f"{LABELS[event]}:{gi}:{hi}",
                        "command": h["command"],
                        "hash": _digest(event, group, h),
                    }
                )
    return out
