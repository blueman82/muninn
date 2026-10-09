"""The ``muninn hook`` command: the provider-facing entry point.

A hook must never fail its provider, so everything here is fail-open and the
exit code is always 0; exit 2 would even block a prompt.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from muninn import hook, hook_cursor, obs, store

__all__ = ["HOOKS", "PROVIDERS", "hook_main"]

PROVIDERS = ("claude", "codex", "cursor")
type HookRunner = Callable[..., dict[str, Any]]
# Tests patch entries in this dict, so it must stay the one shared object.
HOOKS: dict[str, HookRunner] = {
    "session-start": hook.session_start,
    "prompt": hook.prompt_submit,
    "pre-compact": hook_cursor.pre_compact,
}


def _provider(args: Sequence[str]) -> str | None:
    """Return the value of ``--provider V`` or ``--provider=V``, if valid."""
    for n, arg in enumerate(args):
        if arg.startswith("--provider="):
            value = arg.partition("=")[2]
        elif arg == "--provider" and n + 1 < len(args):
            value = args[n + 1]
        else:
            continue
        return value if value in PROVIDERS else None
    return None


def _hook_actor(
    provider: str, payload: Mapping[str, Any], env: Mapping[str, str]
) -> str:
    """Name the caller for the call log, from the hook payload's session."""
    actor = obs.actor(env)
    session = payload.get("session_id")
    if actor == "user" and isinstance(session, str) and session:
        # Strip to word characters: the id lands in a log line and must not
        # carry anything but a short opaque tag.
        return f"{provider}:{re.sub(r'[^\w-]', '', session)[:12] or 'unknown'}"
    return actor


def _read_payload() -> dict[str, Any]:
    """Read the provider's JSON payload from stdin; ``{}`` if unreadable."""
    try:
        return hook.read_input(sys.stdin.buffer)
    except Exception:  # e.g. no stdin at all
        return {}


def _run_hook(
    run: HookRunner,
    payload: dict[str, Any],
    provider: str,
    env: Mapping[str, str],
    trace: dict[str, Any],
) -> dict[str, Any]:
    """Run one hook function, turning any failure into ``{}``."""
    try:
        return run(payload, provider, env, trace=trace)
    except Exception:  # the hook functions are fail-open already
        return {}


def _log_stage(
    event: str,
    provider: str,
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    trace: Mapping[str, Any],
    body: str,
    started: float,
) -> None:
    """Append the hook's stage line to the call log, best effort."""
    stage = {k: v for k, v in trace.items() if k != "skipped"}
    with contextlib.suppress(Exception):  # logging must never fail a hook
        obs.log_call(
            store.data_home(env),
            stage
            | {
                "cmd": f"hook {event}",
                "actor": _hook_actor(provider, payload, env),
                "exit": 0,
                "bytes_out": len(body),
                "ms": round((time.monotonic() - started) * 1000, 1),
            },
            env,
        )


def hook_main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run ``muninn hook EVENT --provider P``.

    The provider's payload arrives on stdin; its hook JSON (or ``{}``) goes
    to stdout.

    Args:
        argv: Arguments after the word ``hook``.
        env: Process environment.

    Returns:
        Always 0, whatever happens.
    """
    started = time.monotonic()
    payload = _read_payload()
    run = HOOKS.get(argv[0]) if argv else None
    provider = _provider(argv[1:])
    trace: dict[str, Any] = {}
    out: dict[str, Any] = {}
    if run and provider:
        out = _run_hook(run, payload, provider, env, trace)
    body = json.dumps(out)
    with contextlib.suppress(OSError):  # a closed pipe must not fail us
        print(body)
        sys.stdout.flush()
    if run and provider and trace.get("skipped") != "disabled":
        _log_stage(argv[0], provider, payload, env, trace, body, started)
    return 0
