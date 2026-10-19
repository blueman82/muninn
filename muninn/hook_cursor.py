"""Cursor-specific hooks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from muninn import (
    cli_core,
    cursor_import,
    hook_context,
    hook_learn,
    ingest,
    store,
)

__all__ = ["pre_compact"]


def _matching_cwd(value: object, roots: list[Path]) -> str | None:
    """Accept only a native absolute directory inside a declared workspace."""
    if not isinstance(value, str) or "\0" in value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    candidate = candidate.resolve()
    return (
        str(candidate)
        if any(candidate.is_relative_to(root) for root in roots)
        else None
    )


def _workspace_cwd(
    payload: Mapping[str, object], env: Mapping[str, str]
) -> str | None:
    """Bind a preCompact scope to the provider's native workspace roots."""
    values = payload.get("workspace_roots")
    if not isinstance(values, list) or not values:
        return None
    roots: list[Path] = []
    for value in cast(list[object], values):
        if not isinstance(value, str) or "\0" in value:
            return None
        path = Path(value)
        if not path.is_absolute():
            return None
        roots.append(path.resolve())
    if len(roots) == 1:
        return str(roots[0])
    return _matching_cwd(payload.get("cwd"), roots) or _matching_cwd(
        env.get("CURSOR_PROJECT_DIR"), roots
    )


def _request(
    data: Mapping[str, object],
    env: Mapping[str, str],
    trace: dict[str, object],
) -> tuple[int, str, str] | None:
    """Validate bounded hook identity and workspace before opening a writer."""
    count = data.get("message_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        trace["skipped"] = "message_count_unavailable"
        return None
    identity = data.get("conversation_id")
    if (
        not isinstance(identity, str)
        or not identity.strip()
        or "\0" in identity
    ):
        trace["skipped"] = "conversation_id_unavailable"
        return None
    cwd = _workspace_cwd(data, env)
    if cwd is None:
        trace["skipped"] = "workspace_unavailable"
        return None
    return count, identity, cwd


def pre_compact(
    payload: object,
    provider: str,
    env: Mapping[str, str],
    *,
    trace: dict[str, object] | None = None,
) -> dict[str, object]:
    """Refresh Cursor history before compaction for later prompt recall.

    Args:
        payload: The provider's hook payload; non-objects count as empty.
        provider: The hook provider. Only Cursor has this hook.
        env: Environment, for the data directory and kill switch.
        trace: If given, receives counts and a skip or error code.

    Returns:
        Cursor's preCompact output, or ``{}`` after a successful refresh.
    """
    trace = {} if trace is None else trace
    if provider != "cursor":
        trace["skipped"] = "provider"
        return {}
    if env.get("MUNINN_HOOK_DISABLE") == "1":
        trace["skipped"] = "disabled"
        return {}
    try:
        request = _request(hook_context.as_payload(payload), env, trace)
        if request is None:
            messages = {
                "message_count_unavailable": (
                    "Muninn skipped the Cursor history refresh because this "
                    "Cursor hook payload has no valid message_count."
                ),
                "conversation_id_unavailable": (
                    "Muninn skipped an unidentified Cursor conversation."
                ),
                "workspace_unavailable": (
                    "Muninn skipped an unbound Cursor workspace."
                ),
            }
            return {"user_message": messages[str(trace["skipped"])]}
        message_count, conversation_id, cwd = request
        database = cursor_import.default_database(
            ingest.provider_home(env), env=env
        )
        if database is None or not database.is_file():
            trace["skipped"] = "missing_cursor_db"
            return {}
        counts = cursor_import.run(
            store.data_home(env),
            database,
            cwd,
            cli_core.WRITER_WAIT_S,
            max_bubbles=message_count,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        trace["error"] = "cursor_import_failed"
        code = (
            "review_enqueue_busy"
            if isinstance(exc, store.BusyError)
            else "unavailable"
        )
        hook_learn.record_capture(
            store.data_home(env), hook_learn.HookCapture(False, (), code), env
        )
        return {
            "user_message": (
                "Muninn could not refresh Cursor history before compaction;"
                " post-compaction recall may be incomplete."
            )
        }
    trace["counts"] = counts
    if counts["conversations_seen"] == 1:
        hook_learn.capture_event(payload, provider, "pre-compact", env, trace)
    else:
        trace["error"] = "cursor_import_empty"
        hook_learn.record_capture(
            store.data_home(env),
            hook_learn.HookCapture(False, (), "source_unavailable"),
            env,
        )
    return {}


def _session_refresh(
    payload: object,
    provider: str,
    env: Mapping[str, str],
    event: str,
    trace: dict[str, object],
) -> bool:
    """Finish one exact import before capture may own its writer root."""
    if env.get("MUNINN_HOOK_DISABLE") == "1":
        trace["skipped"] = "disabled"
        return False
    data = hook_context.as_payload(payload)
    if provider != "cursor" or not hook_learn.valid_event(
        provider, event, data
    ):
        trace["skipped"] = "native_event_unavailable"
        return False
    cwd = _workspace_cwd(data, env)
    if cwd is None:
        trace["skipped"] = "workspace_unavailable"
        return False
    try:
        database = cursor_import.default_database(
            ingest.provider_home(env), env=env
        )
        if database is None or not database.is_file():
            raise ValueError("missing_cursor_db")
        counts = cursor_import.run(
            store.data_home(env),
            database,
            cwd,
            cli_core.WRITER_WAIT_S,
            max_bubbles=None,
            conversation_id=cast(str, data["conversation_id"]),
        )
        trace["counts"] = counts
        if counts["conversations_seen"] != 1:
            raise ValueError("conversation_unavailable")
    except Exception as exc:
        trace["error"] = "cursor_import_failed"
        code = (
            "review_enqueue_busy"
            if isinstance(exc, store.BusyError)
            else "unavailable"
        )
        hook_learn.record_capture(
            store.data_home(env), hook_learn.HookCapture(False, (), code), env
        )
        return False
    return True


def session_end(
    payload: object,
    provider: str,
    env: Mapping[str, str],
    *,
    trace: dict[str, object] | None = None,
) -> dict[str, object]:
    """Import an exact ended conversation without inventing message_count."""
    trace = {} if trace is None else trace
    if _session_refresh(payload, provider, env, "session-end", trace):
        hook_learn.capture_event(payload, provider, "session-end", env, trace)
    return {}


def refresh_start(
    payload: object,
    provider: str,
    env: Mapping[str, str],
    *,
    trace: dict[str, object] | None = None,
) -> str | None:
    """Bind startup context to its workspace independently of history."""
    trace = {} if trace is None else trace
    if env.get("MUNINN_HOOK_DISABLE") == "1":
        trace["skipped"] = "disabled"
        return None
    data = hook_context.as_payload(payload)
    if provider != "cursor" or not hook_learn.valid_event(
        provider, "session-start", data
    ):
        trace["skipped"] = "native_event_unavailable"
        return None
    cwd = _workspace_cwd(data, env)
    if cwd is None:
        trace["skipped"] = "workspace_unavailable"
    return cwd
