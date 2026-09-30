"""pctx provider hooks (design 4.8, 5; spec 9.2, O3, O5b, O8d, A5).

session_start and prompt_submit take the provider's hook payload and return
its hook output: {"hookSpecificOutput": {"hookEventName", "additionalContext"}}
or {}. Whatever comes from the store is framed as untrusted data, redacted,
delimiter-escaped and bounded. A failing store gives a short framed notice
instead of silence (E13); {} only when PCTX_HOOK_DISABLE=1 or the transcript
belongs to a subagent or reviewer thread.
"""

import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from pctx import classify, knowledge, obs, scope, store

BLOCK_LIMIT = 4000  # SessionStart block, characters
RECALL_LIMIT = 1500  # prompt-time recall block, characters
MAX_INPUT = 64 * 1024  # bytes of hook payload read
FIRST_LINE = 1024 * 1024  # bytes of a transcript's first line read
ENTRY_TEXT = 300  # characters of one entry's text in a block
SHOWN = 8  # knowledge entries a SessionStart block may push (O5b)

OPEN = '<pctx-memory source="pctx" trust="untrusted-data"'
CLOSE = "</pctx-memory>"
USAGE = (
    "Before answering about earlier work or re-deciding a recorded choice, run"
    ' `pctx search "<words>"` and open what you cite; record durable owner'
    " decisions with `pctx know add … --quote`."
)
OPEN_HINT = (
    "Open a hit with `pctx open <ref> --context 3`;"
    " browse with `pctx sessions`."
)

# the `<` of a frame delimiter, however spaced or cased (design 4.8)
_FRAME = re.compile(r"(?i)<(?=\s*/?\s*pctx-(?:memory|recall))")


def escape_delimiter(text: str) -> str:
    """Every `<pctx-memory` / `<pctx-recall` opening or closing delimiter
    (any case, any inner spacing) loses its `<`, so a frame closes once."""
    return _FRAME.sub("&lt;", text)


def _clean(text: object, limit: int) -> str:
    """One line, secrets redacted, delimiters escaped, cut to `limit`."""
    flat = " ".join(str(text).split())
    safe = escape_delimiter(classify.redact(flat)[0])
    return safe if len(safe) <= limit else safe[: limit - 1] + "…"


def _entry_line(entry: Mapping) -> str:
    text = _clean(entry["text"], ENTRY_TEXT)
    quote = _clean(entry["quote"], 120)
    return (
        f"- {entry['id']} [{entry['kind']} {entry['date']}"
        f" by:{_clean(entry['actor'], 40)}] {text}"
        f' (quote: "{quote}"; cite: {_clean(entry["cite"], 120)})'
    )


def _frame(attrs: str, lines: list[str]) -> str:
    return "\n".join([f"{OPEN}{attrs}>", *lines, CLOSE])


def render_block(
    entries: list[dict],
    scope_label: str,
    *,
    notes: tuple[str, ...] = (),
    limit: int = BLOCK_LIMIT,
) -> str:
    """The SessionStart block: block_entries output, the usage lines and
    any notes, framed; the last entries are dropped until it fits."""
    label = _clean(scope_label, 80)
    tail = [USAGE, OPEN_HINT, *(_clean(n, 200) for n in notes)]
    for shown in range(len(entries), -1, -1):
        head = []
        if shown:
            head = [
                f"Project knowledge for {label} ({shown} current; cited; may"
                " be stale — verify with `pctx know show K<id>`):",
                *(_entry_line(e) for e in entries[:shown]),
            ]
        block = _frame("", head + tail)
        if len(block) <= limit:
            return block
    return block[: limit - len(CLOSE) - 1] + "\n" + CLOSE


def read_input(stream) -> dict:
    """The hook payload from a binary stream: at most MAX_INPUT bytes of a
    JSON object, else {} (a bad payload must never fail a hook)."""
    try:
        raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return {}
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_record(path: str) -> dict | None:
    """Line 1 of a transcript as a dict: a regular file only, never
    through a symlink, never blocking on a FIFO, at most FIRST_LINE bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        line = handle.readline(FIRST_LINE + 1)
    try:
        record = json.loads(line) if len(line) <= FIRST_LINE else None
    except (ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _subagent(payload: Mapping, provider: str) -> bool:
    """Whether the payload's transcript is a subagent or reviewer thread;
    unknown, unreadable or unclassifiable is treated as a main session."""
    path = payload.get("transcript_path")
    first = _first_record(path) if isinstance(path, str) and path else None
    if first is None:
        return False
    try:
        if provider == "codex":
            info = classify.codex_thread(first)
        else:
            info = classify.claude_thread(path, first)
    except ValueError:
        return False
    return info.thread_class in ("subagent", "reviewer")


def _open(home: Path):
    """A read-only connection; a crashed writer's hot journal is healed
    once when this process may write, else HotJournal."""
    db = store.db_path(home)
    try:
        return store.connect_ro(db)
    except store.HotJournal:
        if not store.heal_hot_journal(db, home):
            raise
        return store.connect_ro(db)


def _notice(code: str) -> str:
    """What a provider sees when the store cannot be read (E13, A5)."""
    return _frame(' kind="notice"', [f"pctx: store unavailable ({code})"])


def _stale(home: Path) -> tuple[str, ...]:
    """A line when the poller's heartbeat is older than 3x its interval."""
    fresh = obs.freshness(obs.read_status(home))
    if fresh["poller"] == "ok":
        return ()
    age = fresh["index_age_s"]
    since = "no heartbeat" if age is None else f"last pass {age}s ago"
    return (
        f"pctx: the index is stale ({since}); recent sessions may be missing.",
    )


def _cwd(payload: Mapping) -> str:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    try:
        return os.getcwd()
    except OSError:
        return ""


def _label(conn, ids: list[int], cwd: str) -> str:
    row = conn.execute(
        "SELECT label FROM scope WHERE kind != 'global' AND id IN"
        f" ({','.join('?' * len(ids))}) LIMIT 1",
        ids,
    ).fetchone()
    return row[0] if row else os.path.basename(cwd) or cwd


def _respond(event, payload, provider, env, trace, build) -> dict:
    """The common path: disabled or subagent -> {}; else build(...) on a
    read-only connection; any failure -> a framed notice (fail-open)."""
    trace = {} if trace is None else trace
    if env.get("PCTX_HOOK_DISABLE") == "1":
        trace["skipped"] = "disabled"
        return {}
    payload = payload if isinstance(payload, Mapping) else {}
    try:
        if _subagent(payload, provider):
            trace["skipped"] = "subagent"
            return {}
    except Exception:  # an odd transcript is not a reason to stay silent
        pass
    try:
        home = store.data_home(env)
        conn = _open(home)
        try:
            text = build(conn, home, payload, env, trace)
        finally:
            conn.close()
    except store.HotJournal:
        trace["error"] = "hot_journal"
        text = _notice("hot_journal")
    except store.StoreUnavailable:
        trace["error"] = "store_unavailable"
        text = _notice("store_unavailable")
    except Exception:
        trace["error"] = "error"
        text = _notice("error")
    if not text:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }


def _start_block(conn, home, payload, env, trace) -> str:
    cwd = _cwd(payload)
    ids = scope.scope_ids_for_read(conn, cwd)
    entries = knowledge.block_entries(conn, ids, limit=SHOWN)
    trace["knowledge_ids"] = [int(e["id"][1:]) for e in entries]
    return render_block(entries, _label(conn, ids, cwd), notes=_stale(home))


def session_start(
    payload: dict, provider: str, env: Mapping[str, str], *, trace=None
) -> dict:
    """SessionStart: the user-cited knowledge of this repo and global
    (spec O5b) plus the usage line (O3). trace, if given, receives counts
    and the skip/error code for the stage log."""
    return _respond(
        "SessionStart", payload, provider, env, trace, _start_block
    )
