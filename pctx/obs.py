"""pctx observability (design 7; spec O8d, O9, A5, A14).

calls.jsonl gets one allowlisted line per CLI call (no text, no query
string), rotated at 1 MiB x 2.  status.json is the poller heartbeat
(counts only), written atomically.  stats and doctor read the store.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

from pctx import query, store

ROTATE_BYTES = 1024 * 1024
LINE_BYTES = 1024
_NUMBERS = (
    "at",
    "scope_id",
    "n_terms",
    "bytes_out",
    "ms",
    "exit",
    "target_id",
    "n_context",
)
_FLAGS = ("hash_ok", "logged")
_ID_LISTS = ("returned_ids", "knowledge_ids", "ids")
_COUNTS = ("stages", "counts")
_CODES = {  # string fields: fixed shapes that cannot carry text
    "cmd": re.compile(r"[a-z][a-z-]{0,30}(?: [a-z-]{1,20})?"),
    "actor": re.compile(r"user|(?:claude|codex):[\w-]{1,12}"),
    "query_sha12": re.compile(r"[0-9a-f]{12}"),
    "error": re.compile(r"[a-z_]{1,40}"),
}
_KEY = re.compile(r"[a-z_]{1,30}")


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean(record: Mapping) -> dict:
    """Only allowlisted fields of allowlisted shapes survive."""
    out = {}
    for key, value in record.items():
        if key in _NUMBERS and (_int(value) or isinstance(value, float)):
            out[key] = value
        elif key in _FLAGS and (value is None or isinstance(value, bool)):
            out[key] = value
        elif key in _CODES and isinstance(value, str):
            if _CODES[key].fullmatch(value):
                out[key] = value
        elif key in _ID_LISTS and isinstance(value, list):
            out[key] = [v for v in value if _int(v)]
        elif key in _COUNTS and isinstance(value, Mapping):
            out[key] = {
                k: v
                for k, v in value.items()
                if isinstance(k, str) and _KEY.fullmatch(k) and _int(v)
            }
    return out


def log_call(
    home: Path, record: Mapping, env: Mapping[str, str] = os.environ
) -> bool:
    """Append one line to calls.jsonl; False when disabled or denied (the
    response then says logged:false).  PCTX_NO_CALLLOG=1 disables it."""
    if env.get("PCTX_NO_CALLLOG") == "1":
        return False
    line = _clean({**record, "at": round(time.time(), 3)})

    def encoded():
        text = json.dumps(line, sort_keys=True, separators=(",", ":"))
        return text.encode() + b"\n"

    data = encoded()
    for key in _ID_LISTS:  # keep the line within LINE_BYTES
        while len(data) > LINE_BYTES and line.get(key):
            line[key] = line[key][: len(line[key]) // 2]
            data = encoded()
    path = home / "calls.jsonl"
    try:
        if path.exists() and path.stat().st_size + len(data) > ROTATE_BYTES:
            os.replace(path, home / "calls.jsonl.1")  # two files at most
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:  # e.g. the Codex sandbox denies the append
        return False
    return True


def actor(env: Mapping[str, str]) -> str:
    """claude:<12> or codex:<12> of the calling session, else user."""
    for name, provider in (
        ("CLAUDE_CODE_SESSION_ID", "claude"),
        ("CODEX_SESSION_ID", "codex"),
        ("CODEX_THREAD_ID", "codex"),
    ):
        if env.get(name):
            ident = re.sub(r"[^\w-]", "", env[name])[:12]
            return f"{provider}:{ident or 'unknown'}"
    return "user"


def read_status(home: Path) -> dict:
    """The parsed status.json, or {} when absent or unreadable."""
    try:
        data = json.loads((home / "status.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(home: Path, fields: Mapping) -> None:
    """Merge fields into status.json atomically (0600)."""
    store.write_json_atomic(home / "status.json", read_status(home) | fields)


def freshness(status: Mapping) -> dict:
    """index_age_s and poller ok|stale (O8d); {} counts as stale."""
    return query._freshness(status or {})


def install_sha(env: Mapping[str, str]) -> str | None:
    """PCTX_INSTALL_SHA, else the pinned code dir that
    ~/.local/lib/provenance-context/current points at."""
    if env.get("PCTX_INSTALL_SHA"):
        return env["PCTX_INSTALL_SHA"]
    home = Path(env.get("HOME") or Path.home())
    current = home / ".local/lib/provenance-context/current"
    return Path(os.readlink(current)).name if current.is_symlink() else None
