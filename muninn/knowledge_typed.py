"""Typed ledger fields: confidence, expiry, sensitivity, links, tags, loop.

All fields are optional on ``know add``.  Validation happens before the write
transaction opens, so a bad value refuses with a stable code and writes
nothing.  A loop scope is a ``dir`` scope whose key is ``loop:<id>``; real
dir keys are absolute paths, so the two cannot collide.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from muninn.knowledge_model import RefusedError, parse_kid

type Confidence = Literal["observed", "reported", "inferred"]
type Sensitivity = Literal["normal", "restricted"]

CONFIDENCE: tuple[Confidence, ...] = ("observed", "reported", "inferred")
SENSITIVITY: tuple[Sensitivity, ...] = ("normal", "restricted")
DEFAULT_SENSITIVITY: Sensitivity = "normal"
TAGS_MAX = 10
LOOP_PREFIX = "loop:"
_TAG = re.compile(r"[a-z0-9_-]{1,32}")
_LOOP = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def valid_loop(loop: str) -> bool:
    """Say whether a loop id is well formed; add and list share this rule."""
    return _LOOP.fullmatch(loop) is not None


def valid_tags(tags: Sequence[str]) -> bool:
    """Say whether tags are well formed; add and list share this rule."""
    return len(tags) <= TAGS_MAX and all(_TAG.fullmatch(t) for t in tags)


@dataclass(frozen=True)
class Typed:
    """The validated optional fields of one new entry.

    Attributes:
        confidence: One of ``CONFIDENCE`` or None when not stated.
        valid_until: Epoch seconds after which the entry is not current.
        sensitivity: One of ``SENSITIVITY``.
        contradicts: Id of an entry this one disagrees with; informational.
        tags: Sorted unique retrieval tags.
        loop: Loop id for a loop scope, else None.
    """

    confidence: Confidence | None = None
    valid_until: float | None = None
    sensitivity: Sensitivity = DEFAULT_SENSITIVITY
    contradicts: int | None = None
    tags: tuple[str, ...] = ()
    loop: str | None = None


def parse_valid_until(value: str, now: float) -> float:
    """Return the epoch seconds of an ISO date or datetime in the future.

    Args:
        value: ``YYYY-MM-DD`` or a full ISO 8601 datetime; no zone means UTC.
        now: Current epoch seconds.

    Returns:
        The expiry as epoch seconds.

    Raises:
        RefusedError: ``bad_valid_until`` if it does not parse or is not
            after ``now``.
    """
    try:
        stamp = datetime.fromisoformat(value.strip())
    except ValueError:
        raise RefusedError("bad_valid_until") from None
    at = stamp.replace(tzinfo=stamp.tzinfo or UTC).timestamp()
    if at <= now:
        raise RefusedError("bad_valid_until", "not in the future")
    return at


@dataclass(frozen=True)
class TypedRequest:
    """The optional fields of ``know add`` as the caller gave them.

    Attributes:
        confidence: Requested confidence label.
        valid_until: Requested expiry as text.
        sensitivity: Requested sensitivity.
        contradicts: Entry id in any form ``parse_kid`` accepts, or None.
        tags: Requested retrieval tags.
        loop: Requested loop scope id.
        global_scope: Whether the global scope was also requested.
    """

    confidence: str | None = None
    valid_until: str | None = None
    sensitivity: str = DEFAULT_SENSITIVITY
    contradicts: str | int | None = None
    tags: Sequence[str] = ()
    loop: str | None = None
    global_scope: bool = False


def validate(raw: TypedRequest, now: float) -> Typed:
    """Check every optional field and return them in one value.

    Args:
        raw: The fields as requested.
        now: Current epoch seconds.

    Returns:
        The validated fields.

    Raises:
        RefusedError: With the code of the first bad field.
    """
    if raw.confidence is not None and raw.confidence not in CONFIDENCE:
        raise RefusedError("bad_confidence")
    if raw.sensitivity not in SENSITIVITY:
        raise RefusedError("bad_sensitivity")
    if not valid_tags(raw.tags):
        raise RefusedError("bad_tags")
    if raw.loop is not None and (raw.global_scope or not valid_loop(raw.loop)):
        raise RefusedError("bad_loop_scope")
    link = None
    if raw.contradicts is not None:
        link = parse_kid(raw.contradicts)
        if link is None:
            raise RefusedError("bad_contradicts")
    until = raw.valid_until
    return Typed(
        raw.confidence,
        None if until is None else parse_valid_until(until, now),
        raw.sensitivity,
        link,
        tuple(sorted(set(raw.tags))),
        raw.loop,
    )


def require_contradicted(conn: sqlite3.Connection, typed: Typed) -> None:
    """Refuse a ``contradicts`` link to an entry that does not exist.

    Raises:
        RefusedError: ``bad_contradicts`` for an unknown entry id.
    """
    if (
        typed.contradicts is not None
        and conn.execute(
            "SELECT 1 FROM knowledge WHERE id = ?", (typed.contradicts,)
        ).fetchone()
        is None
    ):
        raise RefusedError("bad_contradicts", "unknown entry")


def loop_scope_id(
    conn: sqlite3.Connection, loop: str, *, create: bool
) -> int | None:
    """Return the scope id of a loop, creating the row when asked.

    Args:
        conn: Open store connection; a writer when ``create`` is true.
        loop: Validated loop id.
        create: Insert the scope row when it is missing.

    Returns:
        The scope id, or None when it is missing and ``create`` is false.
    """
    key = f"{LOOP_PREFIX}{loop}"
    row = conn.execute("SELECT id FROM scope WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return cast(int, row[0])
    if not create:
        return None
    made = conn.execute(
        "INSERT INTO scope(key, label, kind) VALUES (?, ?, 'dir')", (key, key)
    ).lastrowid
    return cast(int, made)
