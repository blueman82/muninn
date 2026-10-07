"""muninn provider hooks: SessionStart and UserPromptSubmit.

``session_start`` and ``prompt_submit`` take the provider's hook payload and
return its hook output: ``{"hookSpecificOutput": {"hookEventName",
"additionalContext"}}`` or ``{}``. Whatever comes from the store is framed
as untrusted data, redacted, delimiter-escaped and bounded (see
``hook_frame``). A failing store gives a short framed notice instead of
silence; ``{}`` is returned only when ``MUNINN_HOOK_DISABLE=1`` or the
transcript belongs to a subagent or reviewer thread.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from muninn import classify, knowledge, scope, store
from muninn.hook_frame import (
    BLOCK_LIMIT,
    RECALL_LIMIT,
    BlockEntry,
    escape_delimiter,
    frame,
    render_block,
)
from muninn.hook_notes import index_notes
from muninn.hook_recall import (
    MAX_EVENTS,
    MIN_TERMS,
    RecallRequest,
    prompt_terms,
    recall_block,
)
from muninn.platform_io import open_regular

__all__ = [
    "BLOCK_LIMIT",
    "MAX_EVENTS",
    "MIN_TERMS",
    "RECALL_LIMIT",
    "escape_delimiter",
    "prompt_submit",
    "read_input",
    "render_block",
    "session_start",
]

# Codex counts additionalContext in tokens (1500 SessionStart, 500 prompt:
# integrations/codex/hooks/hooks.json) and dense refs and timestamps cost
# about 1.8 characters a token, so its blocks are capped lower.
CODEX_BLOCK_LIMIT = 2800
CODEX_RECALL_LIMIT = 900
MAX_INPUT = 64 * 1024  # bytes of hook payload read
FIRST_LINE = 1024 * 1024  # bytes of a transcript's first line read
SHOWN = 8  # knowledge entries a SessionStart block may push, newest first
RECALL_OFF = "recall.off"  # in the data dir: UserPromptSubmit prints {}


class _Classifier(Protocol):
    """The ``muninn.classify`` calls the hook makes, with typed arguments."""

    def codex_thread(self, meta: dict[str, object]) -> classify.ThreadInfo:
        """Classify a Codex thread from its first record."""
        ...

    def claude_thread(
        self, rel_path: str, first: dict[str, object]
    ) -> classify.ThreadInfo:
        """Classify a Claude transcript from its path and first record."""
        ...


class _Knowledge(Protocol):
    """The ``muninn.knowledge`` call the hook makes, with typed results."""

    def block_entries(
        self, conn: sqlite3.Connection, scope_ids: list[int], limit: int
    ) -> list[BlockEntry]:
        """Current user-cited entries of the scopes, newest first."""
        ...

    def withheld(
        self, conn: sqlite3.Connection, scope_ids: list[int]
    ) -> dict[str, int]:
        """Counts of expired and restricted entries a push left out."""
        ...


# classify and knowledge annotate their dicts bare, which pyright strict
# reads as partly unknown; viewing the modules through these protocols
# types the calls here without touching modules this file does not own.
_CLASSIFY = cast("_Classifier", classify)
_KNOWLEDGE = cast("_Knowledge", knowledge)

# Builds the block text from an open read-only connection.
Builder = Callable[
    [
        sqlite3.Connection,
        Path,
        Mapping[str, object],
        Mapping[str, str],
        dict[str, object],
    ],
    str,
]


def read_input(stream: BinaryIO) -> dict[str, object]:
    """Read the hook payload from a binary stream.

    A bad payload must never fail a hook, so every problem gives ``{}``.

    Args:
        stream: Binary stream, usually stdin's buffer.

    Returns:
        The JSON object, read from at most ``MAX_INPUT`` bytes, else ``{}``.
    """
    try:
        raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return {}
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return {}
    # json.loads gives Any; JSON object keys are always strings.
    return (
        cast("dict[str, object]", payload) if isinstance(payload, dict) else {}
    )


def _as_payload(raw: object) -> Mapping[str, object]:
    """Treat anything that is not a JSON object as an empty payload."""
    return (
        cast("Mapping[str, object]", raw) if isinstance(raw, Mapping) else {}
    )


def _read_first_line(path: str) -> bytes | None:
    """Read line 1 of a regular file, or None if it cannot be read safely."""
    try:
        with open_regular(Path(path)) as handle:
            return handle.readline(FIRST_LINE)
    except (OSError, ValueError):
        return None


def _first_record(path: str) -> dict[str, object] | None:
    """Parse line 1 of a transcript as a JSON object.

    Only a regular file is read, never through a symlink, never blocking on
    a FIFO, and at most ``FIRST_LINE`` bytes.

    Returns:
        The record, or None if the file is unsafe, unreadable or not a
        JSON object.
    """
    line = _read_first_line(path)
    if line is None:
        return None
    try:
        record = json.loads(line)  # a line cut at the cap is not JSON
    except (ValueError, RecursionError):
        return None
    # json.loads gives Any; JSON object keys are always strings.
    return (
        cast("dict[str, object]", record) if isinstance(record, dict) else None
    )


def _subagent(payload: Mapping[str, object], provider: str) -> bool:
    """Whether the transcript is a subagent or reviewer thread.

    Unknown, unreadable or unclassifiable counts as a main session, so a
    classification problem never silences a real session.

    Returns:
        True for a subagent or reviewer thread.
    """
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return False
    first = _first_record(path)
    if first is None:
        return False
    try:
        if provider == "codex":
            info = _CLASSIFY.codex_thread(first)
        else:
            info = _CLASSIFY.claude_thread(path, first)
    except ValueError:
        return False
    return info.thread_class in ("subagent", "reviewer")


def _skip_as_subagent(payload: Mapping[str, object], provider: str) -> bool:
    """Like ``_subagent``, but any failure means "not a subagent"."""
    try:
        return _subagent(payload, provider)
    except Exception:  # an odd transcript is not a reason to stay silent
        return False


def _open(home: Path) -> sqlite3.Connection:
    """Open the store read-only.

    A crashed writer's hot journal is healed once if this process may
    write; otherwise ``HotJournalError`` propagates.

    Returns:
        A read-only connection.

    Raises:
        HotJournalError: If the journal is hot and cannot be healed.
    """
    db = store.db_path(home)
    try:
        return store.connect_ro(db)
    except store.HotJournalError:
        if not store.heal_hot_journal(db, home):
            raise
        return store.connect_ro(db)


def _notice(code: str) -> str:
    """The block a provider sees when the store cannot be read."""
    return frame(' kind="notice"', [f"muninn: store unavailable ({code})"])


def _cwd(payload: Mapping[str, object]) -> str:
    """The payload's working directory, else the process's, else empty."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    try:
        return str(Path.cwd())
    except OSError:  # the directory was deleted under us
        return ""


def _label(conn: sqlite3.Connection, ids: list[int], cwd: str) -> str:
    """Name the scope: its stored label, else the last path component."""
    row = conn.execute(
        "SELECT label FROM scope WHERE kind != 'global' AND id IN"
        f" ({','.join('?' * len(ids))}) LIMIT 1",
        ids,
    ).fetchone()
    # Not Path.name: a trailing slash must give the whole cwd.
    return row[0] if row else cwd.rpartition("/")[2] or cwd


def _build_text(
    build: Builder,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    trace: dict[str, object],
) -> str:
    """Run the builder on a read-only connection.

    Any failure becomes a framed notice: a hook must tell the model that
    memory is unavailable rather than fail or say nothing.

    Returns:
        The block text, or a framed notice describing the failure.
    """
    try:
        home = store.data_home(env)
        conn = _open(home)
        try:
            return build(conn, home, payload, env, trace)
        finally:
            conn.close()
    except store.HotJournalError:
        trace["error"] = "hot_journal"
        return _notice("hot_journal")
    except store.StoreUnavailableError as exc:
        trace["error"] = "store_unavailable"
        trace["detail"] = str(exc)[:80]
        return _notice("store_unavailable")
    except Exception:  # fail open: never break the provider's session
        trace["error"] = "error"
        return _notice("error")


def _respond(
    event: str,
    payload: object,
    provider: str,
    env: Mapping[str, str],
    trace: dict[str, object] | None,
    build: Builder,
) -> dict[str, object]:
    """The path both hooks share.

    Disabled or subagent gives ``{}``; otherwise ``build`` runs and its
    text is wrapped in the provider's hook output.

    Returns:
        The hook output, or ``{}`` when there is nothing to say.
    """
    trace = {} if trace is None else trace
    if env.get("MUNINN_HOOK_DISABLE") == "1":
        trace["skipped"] = "disabled"
        return {}
    data = _as_payload(payload)
    if _skip_as_subagent(data, provider):
        trace["skipped"] = "subagent"
        return {}
    text = _build_text(build, data, env, trace)
    # Popped, not left in the trace: the stage log must never see entry text.
    shown = cast("list[str]", trace.pop("shown", []))
    if not text:
        return {}
    out: dict[str, object] = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }
    line = _status_line(event, trace, shown)
    if line:
        out["systemMessage"] = line
    return out


def _status_line(
    event: str, trace: Mapping[str, object], shown: list[str]
) -> str:
    """What both providers show the human, apart from the model block.

    SessionStart lists every pushed entry in full so the person can see what
    the model was told. Recall stays silent because it runs on every prompt.

    Returns:
        The message, or an empty string when there is nothing to show.
    """
    if "error" in trace:
        return f"muninn: memory unavailable ({trace['error']})"
    if event != "SessionStart":
        return ""
    if not shown:
        return "muninn: memory loaded (no knowledge entries yet)"
    head = f"muninn: memory loaded ({len(shown)} knowledge entries)"
    return "\n".join([head, *shown])


def _printable(text: str) -> str:
    """Replace control characters so stored text cannot drive a terminal."""
    return "".join(c if c.isprintable() else " " for c in text)


def _limits(provider: str) -> tuple[int, int]:
    """The (SessionStart, recall) character caps of a provider's blocks."""
    if provider == "codex":
        return CODEX_BLOCK_LIMIT, CODEX_RECALL_LIMIT
    return BLOCK_LIMIT, RECALL_LIMIT


def _start_block(
    conn: sqlite3.Connection,
    home: Path,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    trace: dict[str, object],
    limit: int,
) -> str:
    """Build the SessionStart block for the payload's repository."""
    del env  # part of the Builder signature; unused here
    cwd = _cwd(payload)
    ids = scope.scope_ids_for_read(conn, cwd)
    entries = _KNOWLEDGE.block_entries(conn, ids, limit=SHOWN)
    trace["knowledge_ids"] = [int(e["id"][1:]) for e in entries]
    # The count is a diagnostic: a store fault must not cost the block, but
    # anything else is a bug and reaches the outer handler.
    with contextlib.suppress(store.StoreUnavailableError, sqlite3.Error):
        trace["withheld"] = _KNOWLEDGE.withheld(conn, ids)
    trace["shown"] = [f"- {e['id']}: {_printable(e['text'])}" for e in entries]
    return render_block(
        entries, _label(conn, ids, cwd), notes=index_notes(home), limit=limit
    )


def session_start(
    payload: object,
    provider: str,
    env: Mapping[str, str],
    *,
    trace: dict[str, object] | None = None,
) -> dict[str, object]:
    """Handle SessionStart: push the repo's user-cited knowledge.

    The block holds this repo's and the global knowledge that carries a
    user citation, plus the usage line. It is capped at ``BLOCK_LIMIT``, or
    ``CODEX_BLOCK_LIMIT`` for Codex.

    Args:
        payload: The provider's hook payload; non-objects count as empty.
        provider: ``claude`` or ``codex``.
        env: Environment, for the data directory and kill switch.
        trace: If given, receives counts and the skip or error code for the
            stage log.

    Returns:
        The provider's hook output, or ``{}``.
    """
    limit = _limits(provider)[0]
    build = partial(_start_block, limit=limit)
    return _respond("SessionStart", payload, provider, env, trace, build)


def _recall_block(
    conn: sqlite3.Connection,
    home: Path,
    payload: Mapping[str, object],
    env: Mapping[str, str],
    trace: dict[str, object],
    terms: list[str],
    limit: int,
) -> str:
    """Build the recall block for the payload's prompt."""
    request = RecallRequest(
        terms, _cwd(payload), partial(index_notes, home), limit
    )
    return recall_block(conn, payload, env, trace, request)


def prompt_submit(
    payload: object,
    provider: str,
    env: Mapping[str, str],
    *,
    trace: dict[str, object] | None = None,
) -> dict[str, object]:
    """Handle UserPromptSubmit: recall for the prompt, or ``{}``.

    Matching knowledge comes first (user-cited entries only, as at
    SessionStart: the others stay pull-only). Then come at most
    ``MAX_EVENTS`` prompt and reply events of this repo, never the caller's
    own session, each matching at least ``MIN_TERMS`` distinct query terms.
    Prompts with fewer terms, slash commands and subagent transcripts
    recall nothing. The block is capped at ``RECALL_LIMIT``, or
    ``CODEX_RECALL_LIMIT`` for Codex.

    Args:
        payload: The provider's hook payload; non-objects count as empty.
        provider: ``claude`` or ``codex``.
        env: Environment, for the data directory and kill switches.
        trace: If given, receives counts and the skip or error code for the
            stage log.

    Returns:
        The provider's hook output, or ``{}``.
    """
    trace = {} if trace is None else trace
    if (store.data_home(env) / RECALL_OFF).exists():  # owner switch
        trace["skipped"] = "recall_off"
        return {}
    terms = prompt_terms(_as_payload(payload), trace)
    if terms is None:  # nothing to recall: the store is not even opened
        return {}
    build = partial(_recall_block, terms=terms, limit=_limits(provider)[1])
    return _respond("UserPromptSubmit", payload, provider, env, trace, build)
