"""Provider hooks for SessionStart and UserPromptSubmit.

Stored context is framed as untrusted data, redacted and bounded.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from muninn import classify, knowledge_push, knowledge_read, scope, store
from muninn.hook_context import as_payload, env_for_provider, project_cwd
from muninn.hook_frame import (
    BLOCK_LIMIT,
    RECALL_LIMIT,
    USAGE,
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
from muninn.hook_retrieval import RECALL_ADVISORY, advisory_output
from muninn.hook_status import failure_notice as _notice
from muninn.hook_status import (
    learning_status,
    printable,
    status_line,
    store_reason,
)
from muninn.hook_status import scope_label as _label
from muninn.platform_io import open_regular
from muninn.query.answers import knowledge_scopes
from muninn.review_recovery import startup_action

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


# The classifier's legacy dictionaries are typed at this call boundary.
_CLASSIFY = cast("_Classifier", classify)

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
            info = _CLASSIFY.claude_thread(Path(path).as_posix(), first)
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
    opened = False
    try:
        home = store.data_home(env)
        conn = _open(home)
        opened = True
        try:
            return build(conn, home, payload, env, trace)
        finally:
            conn.close()
    except store.HotJournalError:
        trace["error"] = "hot_journal"
        return _notice("hot_journal")
    except store.StoreUnavailableError as exc:
        trace["error"] = "store_unavailable"
        trace["detail"] = store_reason(str(exc))
        return _notice("store_unavailable")
    except sqlite3.DatabaseError:
        trace["error"] = "store_unavailable"
        return _notice("store_unavailable")
    except ValueError as exc:
        code = (
            "review_policy_required"
            if str(exc) == "review_policy_required"
            else "context_unavailable" if opened else "store_unavailable"
        )
        trace["error"] = code
        return _notice(code)
    except Exception:  # fail open: never break the provider's session
        code = "context_unavailable" if opened else "store_unavailable"
        trace["error"] = code
        return _notice(code)


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
    data = as_payload(payload)
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
    issue = trace.pop("learning_notice", None)
    if isinstance(issue, str):
        out["systemMessage"] = issue
        return out
    line = status_line(event, trace, shown)
    if line:
        out["systemMessage"] = line
    return out


def _limits(provider: str) -> tuple[int, int]:
    """The (SessionStart, recall) UTF-8 byte caps for provider blocks."""
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
    cwd = project_cwd(payload, env)
    with knowledge_read.read_snapshot(conn):
        issue = startup_action(conn)
        if issue is not None:
            trace["learning_notice"] = learning_status(issue)
        ids = scope.scope_ids_for_read(conn, cwd)
        label = _label(conn, ids, cwd)
        project = next(iter(knowledge_scopes(conn, ids, False)), None)
        if project is None:
            trace["knowledge_ids"] = []
            trace["shown"] = []
            block = frame(
                "",
                [
                    "This project has no indexed project memory yet.",
                    "A later indexing pass can add its project mapping. "
                    "A later startup, resume, clear, or compact can read it.",
                    USAGE,
                ],
            )
            return block if len(block.encode()) <= limit else ""
        context = knowledge_push.knowledge_context(
            conn, project, limit, time.time()
        )
    text = render_block(
        context,
        label,
        notes=index_notes(home),
        limit=limit,
    )
    shown = [line for line in text.splitlines() if line.startswith("- K_")]
    trace["knowledge_ids"] = [line.split()[1] for line in shown]
    trace["knowledge_omitted"] = context.truncated or len(shown) < (
        len(context.notes) + len(context.guides)
    )
    trace["shown"] = [printable(line) for line in shown]
    return text


def session_start(
    payload: object,
    provider: str,
    env: Mapping[str, str],
    *,
    trace: dict[str, object] | None = None,
) -> dict[str, object]:
    """Handle SessionStart: push the mapped project's eligible context.

    The provider's fixed byte cap bounds complete notes, the compact guide
    index and rendered UTF-8 output.

    Args:
        payload: The provider's hook payload; non-objects count as empty.
        provider: ``claude`` or ``codex``.
        env: Environment, for the data directory and kill switch.
        trace: If given, receives counts and the skip or error code for the
            stage log.

    Returns:
        The provider's hook output, or ``{}``.
    """
    env = env_for_provider(env, provider)
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
        terms,
        project_cwd(payload, env),
        partial(index_notes, home),
        limit,
    )
    return recall_block(conn, payload, env, trace, request)


def prompt_submit(
    payload: object,
    provider: str,
    env: Mapping[str, str],
    *,
    trace: dict[str, object] | None = None,
) -> dict[str, object]:
    """Handle UserPromptSubmit in off, advisory or automatic mode.

    Eligible knowledge precedes up to ``MAX_EVENTS`` matching earlier events.
    Short, slash and subagent prompts recall nothing. Advisory sends static
    guidance without a store read; automatic blocks use the provider limit.

    Args:
        payload: The provider's hook payload; non-objects count as empty.
        provider: ``claude`` or ``codex``.
        env: Environment, for the data directory and kill switches.
        trace: If given, receives counts and the skip or error code for the
            stage log.

    Returns:
        The provider's hook output, or ``{}``.
    """
    env = env_for_provider(env, provider)
    trace = {} if trace is None else trace
    if env.get("MUNINN_HOOK_DISABLE") == "1":
        trace["skipped"] = "disabled"
        return {}
    if (store.data_home(env) / RECALL_OFF).exists():  # owner switch
        trace["skipped"] = "recall_off"
        return {}
    if (store.data_home(env) / RECALL_ADVISORY).exists():
        trace["skipped"] = "recall_advisory"
        if _skip_as_subagent(as_payload(payload), provider):
            return {}
        return advisory_output(payload)
    terms = prompt_terms(as_payload(payload), trace)
    if terms is None:  # nothing to recall: the store is not even opened
        return {}
    build = partial(
        _recall_block,
        terms=terms,
        limit=_limits(provider)[1],
    )
    native = dict(as_payload(payload)) | {"muninn_provider": provider}
    out = _respond("UserPromptSubmit", native, provider, env, trace, build)
    if isinstance(batch := trace.get("review_due_batch"), int):
        out["systemMessage"] = (
            "muninn: recall review due. Ask Claude or Codex: "
            f'"Run muninn recall-review --batch {batch}'
            " and follow review_prompt."
            " Show ratings, examples and missing evidence; acknowledge only"
            ' after review."'
        )
    return out
