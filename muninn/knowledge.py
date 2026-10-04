"""The cited knowledge ledger: write side and public entry point.

Every entry carries at least one citation: the immutable identity of a
primary prompt, reply or tool_call event plus a verbatim quote of it,
checked when the entry is written. Writers (``add``, ``retract``) must run
under ``store.writer_lock`` with a ``store.connect_rw`` connection;
``run_add`` and ``run_retract`` do that. Readers (``list_entries``, ``show``,
``check``, ``verify_citation``, ``block_entries``, ``user_cited``) work on a
``connect_ro`` connection. A refused write raises ``RefusedError`` and leaves
nothing behind.

The module is split by responsibility: ``knowledge_model`` (limits, refusals,
text rules), ``knowledge_cite`` (citation checks) and ``knowledge_read``
(readers). Their public names are re-exported here, which is where the rest
of muninn imports them from.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired, TypedDict, Unpack, cast

from muninn import scope, store
from muninn.knowledge_cite import (
    caller_prompt,
    caller_session,
    check_citation,
    require_proposals,
    supersedable,
)
from muninn.knowledge_model import (
    KINDS,
    PULL_ONLY,
    REASON_MAX,
    STATUSES,
    TEXT_MAX,
    Cite,
    RefusedError,
    clean_text,
    parse_kid,
)
from muninn.knowledge_read import (
    block_entries,
    check,
    entry,
    list_entries,
    show,
    user_cited,
    verify_citation,
)
from muninn.knowledge_typed import (
    Typed,
    TypedRequest,
    loop_scope_id,
    require_contradicted,
    validate,
)
from muninn.query import NOTICE, guarded

__all__ = [
    "KINDS",
    "STATUSES",
    "RefusedError",
    "add",
    "block_entries",
    "check",
    "list_entries",
    "retract",
    "run_add",
    "run_retract",
    "show",
    "user_cited",
    "verify_citation",
]


class AddArgs(TypedDict):
    """Keyword arguments of ``add`` and ``run_add``.

    Attributes:
        kind: One of ``KINDS``.
        text: Entry text, 1 to ``TEXT_MAX`` characters once cleaned.
        cites: (event ref, quote) pairs; quotes are 12 to 300 characters,
            except that a shorter quote is accepted when it is the whole text
            of a user prompt (an approval).
        quote_only: A quote to find in the caller's own session prompts.
        supersedes: Entry id this one replaces.
        global_scope: Store in the global scope instead of the repo scope.
        confidence: ``observed``, ``reported`` or ``inferred``.
        valid_until: ISO date or datetime after which the entry is expired.
        sensitivity: ``normal`` or ``restricted`` (never pushed to a prompt).
        contradicts: Entry id this one disagrees with; informational only.
        tags: Retrieval tags, lowercase ``[a-z0-9_-]``.
        loop: Store in the scope of this loop id instead of the repo scope.
        cwd: Working directory that selects the repo scope.
        actor: Who is writing, such as ``claude:abc123``.
        roots: Transcript roots, used to refresh the caller's threads.
        env: Environment naming the calling agent's session.
    """

    kind: str
    text: str
    cites: NotRequired[Sequence[tuple[str, str]]]
    quote_only: NotRequired[str | None]
    supersedes: NotRequired[int | None]
    global_scope: NotRequired[bool]
    confidence: NotRequired[str | None]
    valid_until: NotRequired[str | None]
    sensitivity: NotRequired[str]
    contradicts: NotRequired[str | int | None]
    tags: NotRequired[Sequence[str]]
    loop: NotRequired[str | None]
    cwd: str
    actor: str
    roots: Mapping[str, Path]
    env: Mapping[str, str]


@dataclass(frozen=True)
class _AddRequest:
    """``AddArgs`` with defaults filled in."""

    kind: str
    text: str
    cwd: str
    actor: str
    roots: Mapping[str, Path]
    env: Mapping[str, str]
    cites: Sequence[tuple[str, str]] = ()
    quote_only: str | None = None
    supersedes: int | None = None
    global_scope: bool = False
    confidence: str | None = None
    valid_until: str | None = None
    sensitivity: str = "normal"
    contradicts: str | int | None = None
    tags: Sequence[str] = ()
    loop: str | None = None


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Generator[None]:
    """Run the body in one ``BEGIN IMMEDIATE`` transaction.

    IMMEDIATE takes the write lock up front, so a conflicting writer fails
    here rather than half way through. The connection is in autocommit mode,
    so the transaction is managed with explicit statements; any failure,
    including a failed COMMIT or KeyboardInterrupt, rolls back.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _insert(
    conn: sqlite3.Connection,
    sid: int,
    request: _AddRequest,
    body: str,
    cites: list[Cite],
    old: int | None,
    typed: Typed,
) -> int:
    """Insert the entry, its citations and log rows; return the new id."""
    now = time.time()
    kid = conn.execute(
        "INSERT INTO knowledge(scope_id, kind, text, status, supersedes,"
        " actor, created_at, confidence, valid_until, sensitivity,"
        " contradicts, tags)"
        " VALUES (?, ?, ?, 'current', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            request.kind,
            body,
            old,
            request.actor,
            now,
            typed.confidence,
            typed.valid_until,
            typed.sensitivity,
            typed.contradicts,
            ",".join(typed.tags) or None,
        ),
    ).lastrowid
    conn.executemany(
        "INSERT INTO citation(knowledge_id, provider, thread_id, line, part,"
        " line_sha256, role, kind, ts, quote, span_start, span_end)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (kid, c["provider"], c["thread_id"], c["line"], c["part"],
             c["line_sha256"], c["role"], c["kind"], c["ts"], c["quote"],
             c["span"][0], c["span"][1])
            for c in cites
        ],
    )  # fmt: skip
    logged = [(kid, "add")]
    if old is not None:
        # The replaced entry is closed in the same transaction so there is
        # never a moment with two current versions of one fact.
        conn.execute(
            "UPDATE knowledge SET status = 'superseded', superseded_by = ?"
            " WHERE id = ?",
            (kid, old),
        )
        logged += [(kid, "supersede"), (old, "superseded")]
    conn.executemany(
        "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
        " VALUES (?, ?, ?, ?)",
        [(k, action, request.actor, now) for k, action in logged],
    )
    # sqlite3 types lastrowid as int | None; it is always set after a
    # successful INSERT, so the cast only narrows the type.
    return cast(int, kid)


def _write_entry(
    conn: sqlite3.Connection,
    request: _AddRequest,
    body: str,
    root: str | None,
    typed: Typed,
) -> int:
    """Check the citations and write the entry in one transaction."""
    with _immediate(conn):
        require_contradicted(conn, typed)
        if typed.loop is not None:
            sid = cast(int, loop_scope_id(conn, typed.loop, create=True))
        elif request.global_scope:
            sid = scope.global_scope_id(conn)
        else:
            sid = scope.scope_id(conn, request.cwd)
        found = [check_citation(conn, ref, q) for ref, q in request.cites]
        if request.quote_only is not None and root is not None:
            found.append(caller_prompt(conn, root, request.quote_only))
        # The same event quoted twice is one citation.
        found = list({(c["event"], c["quote"]): c for c in found}.values())
        require_proposals(conn, found)
        if request.kind == "preference" and not any(
            (c["role"], c["kind"]) == ("user", "prompt") for c in found
        ):
            raise RefusedError("preference_needs_user")
        old = None
        if request.supersedes is not None:
            old = supersedable(conn, request.supersedes, sid)
        return _insert(conn, sid, request, body, found, old, typed)


@guarded
def add(conn: sqlite3.Connection, **kwargs: Unpack[AddArgs]) -> dict[str, Any]:
    """Write one cited entry in a single transaction, or refuse.

    Each cite is a (ref, quote) pair; the quote must be 12 to 300 characters
    of the event, whitespace collapsed, unless it is the whole text of a user
    prompt (an approval), which may be shorter. A preference needs a cited
    user prompt.

    Args:
        conn: Read-write connection; the caller holds the writer lock.
        **kwargs: The fields described by ``AddArgs``.

    Returns:
        The notice and the rendered new entry, plus ``pull_only`` when the
        entry will not be pushed (see ``user_cited``).

    Raises:
        RefusedError: If any rule is broken; nothing is written.
        TypeError: If a required field is missing or a field is unknown.
    """
    request = _AddRequest(**kwargs)
    if request.kind not in KINDS:
        raise RefusedError("bad_kind")
    if not request.actor:
        raise RefusedError("bad_actor")
    body = clean_text(request.text, "text_length", 1, TEXT_MAX)
    typed = validate(
        TypedRequest(
            request.confidence,
            request.valid_until,
            request.sensitivity,
            request.contradicts,
            request.tags,
            request.loop,
            request.global_scope,
        ),
        time.time(),
    )
    if not request.cites and request.quote_only is None:
        raise RefusedError("uncited")
    root = caller_session(conn, request.roots, request.env)
    if request.quote_only is not None and root is None:
        raise RefusedError("no_caller_session")
    kid = _write_entry(conn, request, body, root, typed)
    out: dict[str, Any] = {"notice": NOTICE, "entry": entry(conn, kid)}
    if kid not in user_cited(conn, [kid]):
        out["pull_only"] = PULL_ONLY
    return out


@guarded
def retract(
    conn: sqlite3.Connection, kid: int, *, reason: str, actor: str
) -> dict[str, Any]:
    """Retract a current entry; its text stays, with the reason.

    Args:
        conn: Read-write connection; the caller holds the writer lock.
        kid: Entry id in any form ``parse_kid`` accepts.
        reason: Why it is retracted, up to ``REASON_MAX`` characters.
        actor: Who is retracting.

    Returns:
        The notice and the rendered entry.

    Raises:
        RefusedError: If the actor or reason is bad, or the entry is
            unknown or not current.
    """
    if not actor:
        raise RefusedError("bad_actor")
    why = clean_text(reason, "reason_length", 0, REASON_MAX)
    number = parse_kid(kid)
    with _immediate(conn):
        row = (
            conn.execute(
                "SELECT status FROM knowledge WHERE id = ?", (number,)
            ).fetchone()
            if number
            else None
        )
        if row is None:
            raise RefusedError("not_found")
        if row["status"] != "current":
            raise RefusedError("not_current")
        conn.execute(
            "UPDATE knowledge SET status = 'retracted', retract_reason = ?"
            " WHERE id = ?",
            (why or None, number),
        )
        conn.execute(
            "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
            " VALUES (?, 'retract', ?, ?)",
            (number, actor, time.time()),
        )
    # A falsy number made ``row`` None and raised above, so it is an int
    # here; the cast only narrows the type.
    return {"notice": NOTICE, "entry": entry(conn, cast(int, number))}


def run_add(
    home: Path, *, wait_s: float = 15.0, **kwargs: Unpack[AddArgs]
) -> dict[str, Any]:
    """Take the writer lock, open the store durably, and add an entry.

    The store is opened with full fsync so an acknowledged entry survives a
    crash.

    Args:
        home: Directory that holds the store.
        wait_s: Seconds to wait for the writer lock.
        **kwargs: The fields described by ``AddArgs``.

    Returns:
        The result of ``add``.
    """
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            return add(conn, **kwargs)
        finally:
            conn.close()


def run_retract(
    home: Path,
    *,
    kid: int,
    reason: str,
    actor: str,
    wait_s: float = 15.0,
) -> dict[str, Any]:
    """Take the writer lock, open the store durably, and retract an entry.

    Args:
        home: Directory that holds the store.
        kid: Entry id in any form ``parse_kid`` accepts.
        reason: Why it is retracted.
        actor: Who is retracting.
        wait_s: Seconds to wait for the writer lock.

    Returns:
        The result of ``retract``.
    """
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            return retract(conn, kid, reason=reason, actor=actor)
        finally:
            conn.close()
