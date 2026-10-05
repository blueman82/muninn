"""Call and poller logs: one allowlisted JSON line per event.

``calls.jsonl`` gets one line per CLI call and ``poller.log`` one per poller
event. Lines carry numbers, flags, ids and fixed-shape codes only: never
text and never a query string, so the logs cannot leak conversation
content. Both files rotate at 1 MiB and keep one previous generation.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

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
    }
)
_FLAGS = frozenset({"hash_ok", "logged"})
_ID_LISTS = ("returned_ids", "knowledge_ids", "ids")
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
        return [v for v in ids if _is_int(v)]
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
    out: dict[str, object] = {}
    for key, value in record.items():
        cleaned = _clean_value(key, value)
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
        False when logging is disabled or the append was denied (the CLI
        response then reports ``logged: false``).
    """
    if env.get("MUNINN_NO_CALLLOG") == "1":
        return False
    line = clean_record({**record, "at": round(time.time(), 3)})
    data = _encode(line)
    # Halve long id lists until the line fits LINE_BYTES; a truncated list
    # stays valid JSON, unlike cutting the encoded line.
    for key in _ID_LISTS:
        while len(data) > LINE_BYTES and line.get(key):
            # clean_record guarantees a list of ints for these keys.
            ids = cast("list[object]", line[key])
            line[key] = ids[: len(ids) // 2]
            data = _encode(line)
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
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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
