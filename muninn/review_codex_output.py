"""Decode bounded Codex trial events without granting action authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from muninn.review_models import ObservedConfig, Usage
from muninn.review_output import reported_metadata
from muninn.review_process_protocol import (
    finite_float,
    reject_constant,
    unique_object,
)

_TOOL_ITEMS = {
    "command_execution",
    "mcp_tool_call",
    "web_search",
    "file_change",
}
_COUNTERS = {
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
}


def output_contract_digest() -> str:
    """Bind the trial contract to the exact native decoder implementation."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _object(raw: bytes | str) -> dict[str, object]:
    """Reject duplicate keys, nonfinite values and non-object JSON."""
    value: object = json.loads(
        raw,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError("invalid_native_output")
    return cast(dict[str, object], value)


def _usage(value: object) -> Usage | None:
    """Keep unreported cost and observations unknown without estimation."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid_native_output")
    counters = cast(dict[str, object], value)
    if set(counters) - _COUNTERS or any(
        v is not None and (type(v) is not int or v < 0)
        for v in counters.values()
    ):
        raise ValueError("invalid_native_output")
    return reported_metadata(
        {},
        {
            "input_tokens": counters.get("input_tokens"),
            "output_tokens": counters.get("output_tokens"),
            "cached_tokens": counters.get("cached_input_tokens"),
        },
    )[1]


def decode_codex_output(
    raw: bytes, max_output_bytes: int
) -> tuple[bytes, ObservedConfig, Usage | None]:
    """Require one final JSON body followed by one successful native turn."""
    if (
        type(raw) is not bytes
        or type(max_output_bytes) is not int
        or max_output_bytes <= 0
        or len(raw) > max_output_bytes
    ):
        raise ValueError("native_output_limit")
    try:
        events = _events(raw)
        if len(events) != 4:
            raise ValueError("invalid_native_output")
        thread, started, final, completed = events
        if (
            set(thread) != {"type", "thread_id"}
            or thread["type"] != "thread.started"
            or not isinstance(thread["thread_id"], str)
            or not thread["thread_id"]
            or started != {"type": "turn.started"}
            or set(final) != {"type", "item"}
            or final["type"] != "item.completed"
            or completed.get("type") != "turn.completed"
            or set(completed) - {"type", "usage"}
        ):
            raise ValueError("invalid_native_output")
        value = final["item"]
        if not isinstance(value, dict):
            raise ValueError("invalid_native_output")
        item = cast(dict[str, object], value)
        if (
            set(item) != {"id", "type", "text"}
            or item["type"] != "agent_message"
            or not isinstance(item["id"], str)
            or not item["id"]
            or not isinstance(item["text"], str)
        ):
            raise ValueError("invalid_native_output")
        payload = _payload(item["text"], max_output_bytes)
        body = json.dumps(
            _object(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        usage = _usage(completed.get("usage"))
    except (ValueError, RecursionError, UnicodeError) as exc:
        if str(exc) == "native_tool_event":
            raise
        raise ValueError("invalid_native_output") from None
    if len(body) > max_output_bytes:
        raise ValueError("native_output_limit")
    return body, reported_metadata({}, None)[0], usage


def _payload(raw: str, limit: int) -> str:
    """Require a bounded serialized object inside the strict wire envelope."""
    envelope = _object(raw)
    payload = envelope.get("payload_json")
    if set(envelope) != {"payload_json"} or not isinstance(payload, str):
        raise ValueError("invalid_native_output")
    if len(payload.encode()) > limit:
        raise ValueError("native_output_limit")
    return payload


def tool_event_seen(raw: bytes) -> bool:
    """Recognize reported actions before completion, without executing them."""
    for line in raw.splitlines():
        try:
            event = _object(line)
        except (ValueError, RecursionError, UnicodeError):
            continue
        value = event.get("item")
        item = (
            cast(dict[str, object], value) if isinstance(value, dict) else {}
        )
        kind = item.get("type")
        if isinstance(kind, str) and kind in _TOOL_ITEMS:
            return True
    return False


def _events(raw: bytes) -> list[dict[str, object]]:
    """Parse the finite native sequence and retain action-specific failure."""
    events: list[dict[str, object]] = []
    for line in raw.decode("utf-8").splitlines():
        event = _object(line)
        value = event.get("item")
        item = (
            cast(dict[str, object], value) if isinstance(value, dict) else {}
        )
        if isinstance(item.get("type"), str) and item["type"] in _TOOL_ITEMS:
            raise ValueError("native_tool_event")
        events.append(event)
    return events
