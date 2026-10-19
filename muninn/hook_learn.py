"""Persist bounded native lifecycle observations without invoking reviewers."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from muninn import classify, cursor_import, ingest, store
from muninn.cli_learn import installed_policy
from muninn.hook_context import as_payload
from muninn.knowledge_models import Provider, RequestId
from muninn.obs_log import log_call
from muninn.review_evidence_models import CursorOriginal, SourceRef
from muninn.review_models import ReviewPolicy
from muninn.review_originals import originals, reread
from muninn.review_progress import (
    mark_eligible,
    reconciliation_windows,
    source_row,
    unsuppressed_window,
)
from muninn.review_read_budget import ReadBudget
from muninn.review_requests import enqueue_review

_EVENTS = {
    "session-start": "SessionStart",
    "session-end": "SessionEnd",
    "pre-compact": "PreCompact",
}


@dataclass(frozen=True)
class HookCapture:
    """Report internal durable persistence independently of provider output."""

    accepted: bool
    request_ids: tuple[RequestId, ...]
    code: str


def _text(value: object) -> bool:
    """Require nonempty native strings without embedded null bytes."""
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def _absolute(value: object) -> bool:
    """Require a native absolute pathname before any filesystem access."""
    return _text(value) and Path(cast(str, value)).is_absolute()


def _number(value: object) -> bool:
    """Accept finite nonnegative native JSON numbers, excluding booleans."""
    number = cast(float, value)
    return type(value) in (int, float) and 0 <= number < float("inf")


def valid_event(provider: str, event: str, data: Mapping[str, object]) -> bool:
    """Validate supported native lifecycle fields without guessing aliases."""
    native = _EVENTS.get(event)
    if native is None or provider not in {"claude", "codex", "cursor"}:
        return False
    if provider == "cursor":
        return _cursor_event(event, data)
    return _jsonl_event(provider, event, data, native)


def _jsonl_event(
    provider: str, event: str, data: Mapping[str, object], native: str
) -> bool:
    """Check the common fields and distinct Claude/Codex event additions."""
    if (
        data.get("hook_event_name") != native
        or not all(_text(data.get(key)) for key in ("session_id", "cwd"))
        or not _absolute(data.get("cwd"))
        or "transcript_path" not in data
        or (
            data["transcript_path"] is not None
            and not _absolute(data["transcript_path"])
        )
    ):
        return False
    if event == "session-start":
        return data.get("source") in (
            "startup",
            "resume",
            "clear",
            "compact",
        ) and (
            provider != "codex"
            or (
                _text(data.get("model"))
                and data.get("permission_mode")
                in (
                    "default",
                    "acceptEdits",
                    "plan",
                    "dontAsk",
                    "bypassPermissions",
                )
            )
        )
    if event == "session-end":
        reasons = (
            ("other",)
            if provider == "codex"
            else ("clear", "resume", "logout", "prompt_input_exit", "other")
        )
        return data.get("reason") in reasons
    if data.get("trigger") not in ("manual", "auto"):
        return False
    if provider == "codex":
        return all(_text(data.get(key)) for key in ("turn_id", "model"))
    return "custom_instructions" in data and (
        data["custom_instructions"] is None
        or isinstance(data["custom_instructions"], str)
    )


def _cursor_event(event: str, data: Mapping[str, object]) -> bool:
    """Keep Cursor's distinct event schemas and end-without-count contract."""
    native = _EVENTS[event]
    native = native[0].lower() + native[1:]
    roots = data.get("workspace_roots")
    if data.get("hook_event_name") != native or not all(
        _text(data.get(key))
        for key in (
            "conversation_id",
            "generation_id",
            "model",
            "cursor_version",
        )
    ):
        return False
    if (
        not isinstance(roots, list)
        or not roots
        or not all(_absolute(root) for root in cast(list[object], roots))
    ):
        return False
    if event == "pre-compact":
        count = data.get("message_count")
        return (
            type(count) is int
            and count > 0
            and data.get("trigger") in ("auto", "manual")
            and type(data.get("is_first_compaction")) is bool
            and all(
                _number(data.get(key))
                for key in (
                    "context_usage_percent",
                    "context_tokens",
                    "context_window_size",
                    "messages_to_compact",
                )
            )
        )
    if (
        data.get("session_id") != data.get("conversation_id")
        or data.get("is_background_agent") is not False
    ):
        return False
    return event == "session-start" or (
        data.get("reason")
        in ("completed", "aborted", "error", "window_close", "user_close")
        and _number(data.get("duration_ms"))
        and _text(data.get("final_status"))
    )


def _jsonl_source(
    conn: sqlite3.Connection, provider: Provider, data: Mapping[str, object]
) -> sqlite3.Row:
    """Select exactly one canonical indexed root/path within its provider."""
    value = data.get("transcript_path")
    if not _absolute(value):
        raise ValueError("source_unavailable")
    path = Path(cast(str, value))
    if path != path.resolve():
        raise ValueError("source_unavailable")
    rows: list[sqlite3.Row] = []
    for name, root in ingest.default_roots().items():
        if not name.startswith("claude" if provider == "claude" else "codex"):
            continue
        root = root.resolve()
        if path.is_relative_to(root):
            rows.extend(
                conn.execute(
                    "SELECT * FROM source WHERE provider=? "
                    "AND root=? AND path=?",
                    (provider, name, path.relative_to(root).as_posix()),
                ).fetchall()
            )
    if len(rows) != 1:
        raise ValueError("source_unavailable")
    return source_row(
        conn, SourceRef(provider=provider, thread_id=rows[0]["thread_id"])
    )


def _cursor_source(
    conn: sqlite3.Connection, data: Mapping[str, object]
) -> tuple[sqlite3.Row, Path]:
    """Revalidate the configured exact imported database and conversation."""
    database = cursor_import.default_database(
        ingest.provider_home(os.environ), env=os.environ
    )
    if database is None:
        raise ValueError("source_unavailable")
    source = source_row(
        conn,
        SourceRef(
            provider="cursor", thread_id=cast(str, data["conversation_id"])
        ),
    )
    if (
        source["root"] != "cursor-imports"
        or source["path"] != f"{database.resolve()}#{source['thread_id']}"
    ):
        raise ValueError("source_unavailable")
    return source, database


def _metadata(
    conn: sqlite3.Connection,
    provider: Provider,
    data: Mapping[str, object],
    budget: ReadBudget,
) -> sqlite3.Row:
    """Bind admission to original metadata and its indexed hash."""
    if provider == "cursor":
        source, database = _cursor_source(conn, data)
        with ExitStack() as stack:
            raw = reread(
                stack,
                {},
                CursorOriginal(
                    kind="cursor_db",
                    database=database,
                    conversation_id=source["thread_id"],
                    record_key=f"composerData:{source['thread_id']}",
                ),
                budget.bytes_left,
                budget=budget,
            )
    else:
        source = _jsonl_source(conn, provider, data)
        raw = originals(conn, source, 1, 1, budget.bytes_left, budget=budget)[
            0
        ][1]
    if classify.record_hash(raw) != source["first_line_sha256"]:
        raise ValueError("evidence_changed")
    first = as_payload(json.loads(raw))
    if provider == "cursor":
        if first.get("composerId", source["thread_id"]) != source["thread_id"]:
            raise ValueError("source_unavailable")
        return source
    if provider == "codex":
        meta = as_payload(first.get("payload"))
        info = classify.codex_thread(dict(first))
        descendant = isinstance(
            meta.get("source"), dict
        ) and "subagent" in cast(dict[str, object], meta["source"])
        if (
            meta.get("session_id") != data["session_id"]
            or descendant
            or info.thread_class != "primary"
            or info.forked_from_id is not None
            or info.parent_thread_id is not None
            or "agent_id" in data
        ):
            raise ValueError("source_unavailable")
    else:
        info = classify.claude_thread(source["path"], dict(first))
        if (
            first.get("sessionId") != data["session_id"]
            or info.thread_class != "primary"
        ):
            raise ValueError("source_unavailable")
    return source


def _persist(
    conn: sqlite3.Connection,
    provider: Provider,
    data: Mapping[str, object],
    policy: ReviewPolicy,
) -> tuple[RequestId, ...]:
    """Recheck active policy and admit complete windows in one transaction."""
    if installed_policy(conn) != policy:
        raise ValueError("review_policy_required")
    budget = ReadBudget(policy.max_input_bytes)
    source = _metadata(conn, provider, data, budget)
    now = time.time()
    ids: list[RequestId] = []
    for window, scope in reconciliation_windows(
        conn, policy, now, source, budget=budget
    ):
        permitted = unsuppressed_window(conn, window)
        if permitted is None:
            mark_eligible(conn, window, now)
            continue
        request = enqueue_review(
            conn, permitted, scope, policy, "hook", now, budget=budget
        )
        if request.state != "cancelled":
            ids.append(request.id)
    return tuple(ids)


def capture_boundary(
    home: Path,
    provider: Provider,
    event: str,
    payload: bytes,
    policy: ReviewPolicy,
) -> HookCapture:
    """Capture after Cursor callers finish one successful exact import."""
    try:
        if len(payload) > 64 * 1024:
            raise ValueError("rejected_validation")
        data = as_payload(json.loads(payload))
        if event not in {"session-end", "pre-compact"} or not valid_event(
            provider, event, data
        ):
            raise ValueError("rejected_validation")
        with (
            store.writer_lock(home, wait_s=0),
            closing(store.connect_rw(store.db_path(home))) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            try:
                ids = _persist(conn, provider, data, policy)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return HookCapture(bool(ids), ids, "persisted" if ids else "empty")
    except ValueError as exc:
        code = str(exc)
        allowed = {
            "source_unavailable",
            "original_unavailable",
            "evidence_changed",
            "evidence_erased",
            "evidence_limit",
            "incomplete_window",
            "rejected_scope",
            "review_policy_required",
            "rejected_validation",
        }
        return HookCapture(
            False, (), code if code in allowed else "rejected_validation"
        )
    except (
        store.BusyError,
        OSError,
        sqlite3.Error,
        store.StoreUnavailableError,
    ) as exc:
        return failed_capture(exc)


def failed_capture(exc: Exception) -> HookCapture:
    """Preserve finite contention codes across wrapped store-read failures."""
    cause = exc.__cause__ or exc
    busy = isinstance(exc, store.BusyError) or getattr(
        cause, "sqlite_errorcode", None
    ) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return HookCapture(
        False, (), "review_enqueue_busy" if busy else "unavailable"
    )


def capture_event(
    payload: object,
    provider: str,
    event: str,
    env: Mapping[str, str],
    trace: dict[str, object],
) -> None:
    """Load the policy then capture and log only content-free persistence."""
    if env.get("MUNINN_HOOK_DISABLE") == "1":
        trace["skipped"] = "disabled"
        return
    home = store.data_home(env)
    try:
        with closing(store.connect_ro(store.db_path(home))) as conn:
            policy = installed_policy(conn)
        result = capture_boundary(
            home,
            cast(Provider, provider),
            event,
            json.dumps(as_payload(payload)).encode(),
            policy,
        )
    except Exception as exc:
        result = failed_capture(exc)
    if not result.accepted:
        trace["error"] = result.code
    record_capture(home, result, env)


def record_capture(
    home: Path, result: HookCapture, env: Mapping[str, str]
) -> None:
    """Record persistence and missed-boundary counters without content."""
    errors = {
        "review_policy_required",
        "rejected_scope",
        "rejected_validation",
    }
    record: dict[str, object] = {
        "event": (
            "review_enqueue_busy"
            if result.code == "review_enqueue_busy"
            else "review_enqueued"
        ),
        "accepted": result.accepted,
    }
    if not result.accepted and result.code != "empty":
        record.update(
            error_code=result.code if result.code in errors else "unavailable",
            missed_acknowledgments=1,
            recovery_gaps=1,
        )
    with contextlib.suppress(Exception):
        log_call(home, record, env)
