"""Read canonical erasure intent without changing its durable records."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Generator, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast

from muninn import platform_io
from muninn.tombstone_key import KEYED_PREFIX, TOMBSTONE_FILE, load_key

KEY_FIELDS = (
    "provider",
    "level",
    "session_root",
    "thread_id",
    "line",
    "line_sha256",
)
type IntentKey = tuple[
    str, str, str | None, str | None, int | None, str | None
]


class TombstoneRow(TypedDict):
    """One public tombstone as stored in the table and JSON-lines journal."""

    provider: str
    level: Literal["session", "thread", "line"]
    session_root: str | None
    thread_id: str | None
    line: int | None
    line_sha256: str | None
    created_at: float


class IntentRow(NamedTuple):
    """Hold a validated immutable record with normalized optional metadata."""

    provider: str
    level: str
    session_root: str | None
    thread_id: str | None
    line: int | None
    line_sha256: str | None
    created_at: float

    @property
    def key(self) -> IntentKey:
        """The existing six-field semantic identity."""
        return self[:6]


@dataclass(frozen=True)
class TombstoneIntent:
    """Keep the complete union and missing keys in one read-only snapshot."""

    rows: tuple[IntentRow, ...]
    missing: tuple[IntentRow, ...]
    content_key: bytes | None


def role_digest(role: str, text: str) -> bytes:
    """Return an ephemeral digest of role and text for family comparisons."""
    return hashlib.sha256(
        f"{role}\n{text}".encode("utf-8", "surrogatepass")
    ).digest()


def event_tag(key: bytes, role: str, text: str) -> str:
    """Return the keyed content identity without exposing guessable hashes."""
    data = f"{role}\n{text}".encode("utf-8", "surrogatepass")
    return KEYED_PREFIX + hmac.new(key, data, hashlib.sha256).hexdigest()


def _identifier(value: object) -> str | None:
    """Check optional identity strings without coercing malformed values."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("erasure_intent_invalid") from None
        return value
    raise ValueError("erasure_intent_invalid")


def _timestamp(value: object) -> float:
    """Normalize absent legacy timestamps while rejecting nonfinite values."""
    if value is None:
        return 0.0
    if type(value) not in (int, float):
        raise ValueError("erasure_intent_invalid")
    try:
        result = float(cast("int | float", value))
    except OverflowError:
        raise ValueError("erasure_intent_invalid") from None
    if not math.isfinite(result):
        raise ValueError("erasure_intent_invalid")
    return result


def canonical_record(row: Mapping[str, object]) -> IntentRow:
    """Validate one existing record shape without rewriting its encoding."""
    if set(row) - {*KEY_FIELDS, "created_at"}:
        raise ValueError("erasure_intent_invalid")
    provider, level = row.get("provider"), row.get("level")
    if provider not in ("codex", "claude", "cursor") or level not in (
        "session",
        "thread",
        "line",
    ):
        raise ValueError("erasure_intent_invalid")
    session, thread = _identifier(row.get("session_root")), _identifier(
        row.get("thread_id")
    )
    line, digest = row.get("line"), _identifier(row.get("line_sha256"))
    if line is not None and (type(line) is not int or not 0 <= line < 2**63):
        raise ValueError("erasure_intent_invalid")
    if (
        digest is not None
        and re.fullmatch(r"(?:ev2:)?[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("erasure_intent_invalid")
    valid = {
        "session": session is not None,
        "thread": thread is not None,
        "line": thread is not None and line is not None and digest is not None,
    }
    if not valid[cast("str", level)]:
        raise ValueError("erasure_intent_invalid")
    if level == "line" and bool(line == 0) != bool(
        digest and digest.startswith(KEYED_PREFIX)
    ):
        raise ValueError("erasure_intent_invalid")
    return IntentRow(
        cast("str", provider),
        cast("str", level),
        session,
        thread,
        line,
        digest,
        _timestamp(row.get("created_at")),
    )


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate object keys before their values can be overwritten."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("erasure_intent_invalid")
        result[key] = value
    return result


def read_journal(home: Path) -> Generator[IntentRow]:
    """Read every private journal record, refusing torn or malformed lines."""
    path = home / TOMBSTONE_FILE
    try:
        discovered = path.lstat()
    except FileNotFoundError:
        # Windows also reports a missing child when its parent is a file.
        if not stat.S_ISDIR(home.stat().st_mode):
            raise NotADirectoryError("erasure_journal_parent") from None
        return
    if stat.S_ISDIR(discovered.st_mode):
        raise IsADirectoryError("erasure_journal_directory")
    with platform_io.open_regular(
        path, root=home, expected=discovered
    ) as handle:
        platform_io.assert_private_fd(handle.fileno())
        if os.name != "nt" and os.fstat(handle.fileno()).st_mode & 0o077:
            raise PermissionError("erasure_journal_permissions")
        try:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    raise ValueError("erasure_intent_invalid")
                row: object = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=_object
                )
                if not isinstance(row, dict):
                    raise ValueError("erasure_intent_invalid")
                yield canonical_record(cast("dict[str, object]", row))
        except (ValueError, RecursionError):
            raise ValueError("erasure_intent_invalid") from None


def read_intent(conn: sqlite3.Connection, home: Path) -> TombstoneIntent:
    """Read strict journal/table intent with no schema or key creation.

    The caller validates its supported schema and owns any snapshot or writer
    scope. Ordinary filesystem and SQLite errors retain their original types.

    Args:
        conn: Caller-owned connection, including a checked legacy source.
        home: Directory of the durable journal and secret content key.

    Returns:
        Deduplicated immutable intent and its not-yet-inserted records.
    """
    fields = (*KEY_FIELDS, "created_at")
    rows = {
        record.key: record
        for values in conn.execute(
            f"SELECT {', '.join(fields)} FROM tombstone"
        )
        for record in (
            canonical_record(dict(zip(fields, values, strict=True))),
        )
    }
    missing: list[IntentRow] = []
    # Keep only distinct intent; a repeated journal entry still needs parsing.
    with closing(read_journal(home)) as journal:
        for record in journal:
            if record.key not in rows:
                rows[record.key] = record
                missing.append(record)
    key = (
        load_key(home, create=False)
        if any(
            r.line_sha256 and r.line_sha256.startswith(KEYED_PREFIX)
            for r in rows.values()
        )
        else None
    )
    return TombstoneIntent(
        tuple(rows.values()),
        tuple(missing),
        key,
    )
