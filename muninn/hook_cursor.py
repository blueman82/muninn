"""Cursor-specific hooks."""

from __future__ import annotations

from collections.abc import Mapping

from muninn import cli_core, cursor_import, hook_context, ingest, store

__all__ = ["pre_compact"]


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
        message_count = hook_context.as_payload(payload).get("message_count")
        if (
            not isinstance(message_count, int)
            or isinstance(message_count, bool)
            or message_count < 1
        ):
            trace["skipped"] = "message_count_unavailable"
            return {
                "user_message": (
                    "Muninn skipped the Cursor history refresh because this "
                    "Cursor hook payload has no valid message_count."
                )
            }
        database = cursor_import.default_database(
            ingest.provider_home(env), env=env
        )
        if database is None or not database.is_file():
            trace["skipped"] = "missing_cursor_db"
            return {}
        cwd = hook_context.project_cwd(hook_context.as_payload(payload), env)
        counts = cursor_import.run(
            store.data_home(env),
            database,
            cwd,
            cli_core.WRITER_WAIT_S,
            max_bubbles=message_count,
        )
    except Exception:
        trace["error"] = "cursor_import_failed"
        return {
            "user_message": (
                "Muninn could not refresh Cursor history before compaction;"
                " post-compaction recall may be incomplete."
            )
        }
    trace["counts"] = counts
    return {}
