"""Call and poller logs: one allowlisted JSON line per event.

``calls.jsonl`` gets one line per CLI call and ``poller.log`` one per poller
event. Lines carry numbers, flags, ids and fixed-shape codes only: never
text and never a query string, so the logs cannot leak conversation
content. Both files rotate at 1 MiB and keep one previous generation.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from muninn import platform_io

ROTATE_BYTES = 1024 * 1024
LINE_BYTES = 1024
_NUMBERS = frozenset(
    {
        "at",
        "scope_id",
        "n_terms",
        "bytes_out",
        "ms",
        "exit",
        "target_id",
        "n_context",
        "pid",
        "passes",
        "busy_skips",
        "interval_s",
        "files_changed",
        "events_added",
        "failed",
        "duration_s",
        "rows",
        "from_v",
        "to_v",
        "delivery_id",
        "review_error",
        "review_due_batch",
    }
)
_FLAGS = frozenset({"hash_ok", "logged"})
_ID_LISTS = (
    "returned_ids",
    "knowledge_ids",
    "ids",
    "delivered_ids",
    "delivered_knowledge_ids",
)
_COUNTS = frozenset({"stages", "counts", "withheld"})
# String fields are accepted only in shapes that cannot carry free text.
_CODES = {
    "cmd": re.compile(r"[a-z][a-z-]{0,30}(?: [a-z-]{1,20})?"),
    "actor": re.compile(r"user|(?:claude|codex):[\w-]{1,12}"),
    "query_sha12": re.compile(r"[0-9a-f]{12}"),
    "error": re.compile(r"[a-z_]{1,40}"),
    "detail": re.compile(r"[A-Za-z0-9_ ,()'.-]{1,80}"),
    "event": re.compile(r"[a-z_]{1,20}"),
    "exc": re.compile(r"[A-Za-z_]{1,60}"),  # an exception class name
}
_KEY = re.compile(r"[a-z_]{1,30}")
_DROP = object()  # marks a field that failed its allowlist

_REVIEW_EVENTS = frozenset(
    {
        "review_enqueue_busy",
        "review_enqueued",
        "review_capture_recovered",
        "review_attempt_started",
        "review_attempt_progress",
        "review_attempt_completed",
        "review_attempt_failed",
        "review_attempt_cancelled",
        "review_attempt_abandoned",
        "review_call_reserved",
        "review_call_launched",
        "review_call_completed",
        "review_call_unknown",
        "review_commit_rejected",
    }
)
_REVIEW_ERRORS = frozenset(
    {
        "rejected_validation",
        "rejected_evidence",
        "rejected_scope",
        "rejected_authority",
        "rejected_revision",
        "rejected_erased",
        "rejected_fenced",
        "auth",
        "config",
        "evidence_limit",
        "deadline",
        "budget",
        "unavailable",
        "review_policy_required",
    }
)
_REVIEW_STATES = frozenset(
    {
        "pending",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
        "reserved",
        "launched",
        "unknown",
    }
)
_REVIEW_CHOICES = {
    "event": _REVIEW_EVENTS,
    "error_code": _REVIEW_ERRORS,
    "state": _REVIEW_STATES,
    "outcome": frozenset({"completed", "completed_unresolved", "no_change"}),
}
_REVIEW_IDS = {
    f"{name}_id": re.compile(f"{prefix}_[0-9a-f]{{32}}")
    for name, prefix in (
        ("request", "R"),
        ("attempt", "A"),
        ("knowledge", "K"),
        ("evidence", "E"),
        ("unresolved", "U"),
    )
}
_REVIEW_ID_LISTS = ("knowledge_ids", "evidence_ids", "unresolved_ids")
_REVIEW_INTS = {
    "schema_version": 1,
    "generation": 1,
    "round_index": 1,
    "revision": 1,
    "pid": 1,
    "eligible_records": 0,
    "completed_records": 0,
    "unresolved_items": 0,
    "observed_enqueue_busy": 0,
    "missed_acknowledgments": 0,
    "recovery_gaps": 0,
}
_REVIEW_TIMES = frozenset(
    {
        "at",
        "started_at",
        "progress_at",
        "ended_at",
        "reserved_at",
        "launched_at",
        "committed_at",
        "elapsed_seconds",
        "deadline_seconds",
        "commit_delay_seconds",
        "cancel_delay_seconds",
        "oldest_pending_age_seconds",
        "last_progress_age_seconds",
    }
)
_REVIEW_FLAGS = frozenset({"accepted", "busy_count_complete"})
_REVIEW_COUNT_KEYS = _REVIEW_STATES | frozenset(_REVIEW_INTS)
_USAGE_TOKENS = frozenset({"input_tokens", "output_tokens", "cached_tokens"})
_USAGE_SHAPES = {
    "cost_decimal": re.compile(r"[0-9]{1,20}(?:\.[0-9]{1,12})?"),
    "currency": re.compile(r"[A-Z]{3}"),
}


def _clean_usage(value: object) -> object:
    """Keep bounded reported usage, preserving explicitly unknown values."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return _DROP
    usage = cast("Mapping[object, object]", value)
    out: dict[str, object] = {}
    for key, measurement in usage.items():
        if not isinstance(key, str):
            continue
        if key in _USAGE_TOKENS:
            valid = _is_int(measurement) and cast("int", measurement) >= 0
        elif key in _USAGE_SHAPES:
            valid = isinstance(measurement, str) and bool(
                _USAGE_SHAPES[key].fullmatch(measurement)
            )
        else:
            continue
        if measurement is None or valid:
            out[key] = measurement
    return out


def _clean_review_measurement(key: str, value: object) -> object:
    """Keep finite nonnegative review counts, timings and flags."""
    if key in _REVIEW_FLAGS:
        return value if isinstance(value, bool) else _DROP
    if key in _REVIEW_INTS:
        if value is None and key == "observed_enqueue_busy":
            return None
        if not _is_int(value) or cast("int", value) < _REVIEW_INTS[key]:
            return _DROP
        return _DROP if key == "schema_version" and value != 1 else value
    if key in _REVIEW_TIMES and (
        value is None
        or (_is_int(value) and cast("int", value) >= 0)
        or (isinstance(value, float) and math.isfinite(value) and value >= 0)
    ):
        return value
    return _DROP


def _clean_review_value(key: str, value: object) -> object:
    """Keep only explicit review codes, identities and measurements."""
    if key in _REVIEW_CHOICES:
        valid = isinstance(value, str) and value in _REVIEW_CHOICES[key]
        nullable = value is None and key in {"error_code", "outcome"}
        return value if valid or nullable else _DROP
    if key in _REVIEW_IDS:
        valid = isinstance(value, str) and _REVIEW_IDS[key].fullmatch(value)
        return value if valid else _DROP
    if key in _REVIEW_ID_LISTS and isinstance(value, list):
        ids = cast("list[object]", value)
        shape = _REVIEW_IDS[key[:-1]]
        return [v for v in ids if isinstance(v, str) and shape.fullmatch(v)]
    if key == "counts" and isinstance(value, Mapping):
        counts = cast("Mapping[object, object]", value)
        return {
            k: v
            for k, v in counts.items()
            if isinstance(k, str)
            and k in _REVIEW_COUNT_KEYS
            and _is_int(v)
            and cast("int", v) >= 0
        }
    if key == "usage":
        return _clean_usage(value)
    return _clean_review_measurement(key, value)


def _is_int(value: object) -> bool:
    """Whether value is an int; bool is excluded though it subclasses int."""
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_value(key: str, value: object) -> object:
    """Return the allowlisted form of one field, or ``_DROP``."""
    if key in _NUMBERS:
        return value if _is_int(value) or isinstance(value, float) else _DROP
    if key in _FLAGS:
        return value if value is None or isinstance(value, bool) else _DROP
    if key in _CODES:
        ok = isinstance(value, str) and _CODES[key].fullmatch(value)
        return value if ok else _DROP
    if key in _ID_LISTS and isinstance(value, list):
        # isinstance leaves the element type unknown; any element is
        # re-checked below.
        ids = cast("list[object]", value)
        return (
            _clean_review_value("knowledge_ids", ids)
            if key in {"knowledge_ids", "delivered_knowledge_ids"}
            else [v for v in ids if _is_int(v)]
        )
    if key in _COUNTS and isinstance(value, Mapping):
        counts = cast("Mapping[object, object]", value)
        return {
            k: v
            for k, v in counts.items()
            if isinstance(k, str) and _KEY.fullmatch(k) and _is_int(v)
        }
    return _DROP


def clean_record(record: Mapping[str, object]) -> dict[str, object]:
    """Keep only allowlisted fields of allowlisted shapes.

    Args:
        record: Candidate log fields.

    Returns:
        The fields that passed; everything else is dropped silently.
    """
    event = record.get("event")
    review = isinstance(event, str) and event.startswith("review_")
    if review and event not in _REVIEW_EVENTS:
        return {}
    out: dict[str, object] = {}
    for key, value in record.items():
        cleaned = (
            _clean_review_value(key, value)
            if review
            else _clean_value(key, value)
        )
        if cleaned is not _DROP:
            out[key] = cleaned
    return out


def _encode(line: Mapping[str, object]) -> bytes:
    """Serialise one log line, compact and key-sorted, newline-terminated."""
    text = json.dumps(line, sort_keys=True, separators=(",", ":"))
    return text.encode() + b"\n"


def log_call(
    home: Path,
    record: Mapping[str, object],
    env: Mapping[str, str] = os.environ,
) -> bool:
    """Append one line to ``calls.jsonl``.

    Args:
        home: Data directory.
        record: Fields to log; non-allowlisted ones are dropped.
        env: Environment; ``MUNINN_NO_CALLLOG=1`` disables logging.

    Returns:
        False when logging is disabled, the record cannot fit the line
        bound, or the append was denied (the CLI reports ``logged: false``).
    """
    if env.get("MUNINN_NO_CALLLOG") == "1":
        return False
    line = clean_record({**record, "at": round(time.time(), 3)})
    data = _encode(line)
    # Halve long id lists until the line fits LINE_BYTES; a truncated list
    # stays valid JSON, unlike cutting the encoded line.
    for key in (*_ID_LISTS, *_REVIEW_ID_LISTS):
        while len(data) > LINE_BYTES and line.get(key):
            # clean_record guarantees an identity list for these keys.
            ids = cast("list[object]", line[key])
            line[key] = ids[: len(ids) // 2]
            data = _encode(line)
    if len(data) > LINE_BYTES:
        if line.get("event") not in _REVIEW_EVENTS:
            return False
        # Overflow drops optional measurements without changing reported
        # usage or cutting the serialised JSON.
        summary_keys = (
            _REVIEW_CHOICES.keys()
            | _REVIEW_IDS.keys()
            | _REVIEW_FLAGS
            | {"at", "usage"}
        )
        line = {
            key: value for key, value in line.items() if key in summary_keys
        }
        data = _encode(line)
        if len(data) > LINE_BYTES:
            return False
    return _append(home, "calls.jsonl", data)


def _append(home: Path, name: str, data: bytes) -> bool:
    """Append to ``home/name``, rotating to ``name.1`` at ``ROTATE_BYTES``.

    At most two generations exist, which bounds disk use. Rotation is not
    locked, so racing processes can both rotate and overwrite ``name.1``,
    or lose the race and hit a missing file in the replace, in which case
    that one line is dropped and False is returned.

    Returns:
        False when the append is denied.
    """
    path = home / name
    try:
        if path.exists() and path.stat().st_size + len(data) > ROTATE_BYTES:
            path.replace(home / f"{name}.1")
        # os.open: the 0600 mode and O_APPEND must apply atomically at open.
        fd = platform_io.open_private(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
        )
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:  # e.g. the Codex sandbox denies the append
        return False
    return True


def log_poller(home: Path, record: Mapping[str, object]) -> bool:
    """Append one line to ``poller.log``.

    The line holds an event code, counts and an exception class name,
    never transcript text. It rotates like ``calls.jsonl``.

    Args:
        home: Data directory.
        record: Fields to log; non-allowlisted ones are dropped.

    Returns:
        False when the append was denied.
    """
    line = clean_record({**record, "at": round(time.time(), 3)})
    return _append(home, "poller.log", _encode(line))


def actor(env: Mapping[str, str]) -> str:
    r"""Name the calling session as ``claude:<12>`` or ``codex:<12>``.

    Args:
        env: Environment holding the provider's session id variable.

    Returns:
        ``user`` when no session id is set. The id is stripped to
        ``[\w-]`` and cut to 12 characters so it fits the log allowlist.
    """
    for name, provider in (
        ("CLAUDE_CODE_SESSION_ID", "claude"),
        ("CODEX_SESSION_ID", "codex"),
        ("CODEX_THREAD_ID", "codex"),
    ):
        if env.get(name):
            ident = re.sub(r"[^\w-]", "", env[name])[:12]
            return f"{provider}:{ident or 'unknown'}"
    return "user"
