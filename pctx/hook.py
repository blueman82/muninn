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
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from pctx import classify, knowledge, obs, query, scope, store

BLOCK_LIMIT = 4000  # SessionStart block, characters
RECALL_LIMIT = 1500  # prompt-time recall block, characters
MAX_INPUT = 64 * 1024  # bytes of hook payload read
FIRST_LINE = 1024 * 1024  # bytes of a transcript's first line read
ENTRY_TEXT = 300  # characters of one entry's text in a block
SHOWN = 8  # knowledge entries a SessionStart block may push (O5b)
MIN_TERMS = 3  # a prompt with fewer query terms recalls nothing (spec 9.2)
MAX_EVENTS = 3  # events in a recall block
EVENT_KINDS = frozenset({"prompt", "reply"})  # recalled at prompt time
POOL_PAGE, POOL_PAGES = 5, 4  # search pages read to find MAX_EVENTS
SNIPPET = 300  # characters of one recalled snippet

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
RECALL_HINT = "Open a hit with `pctx open <ref> --context 3`."

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
    except (OSError, ValueError):  # ValueError: a NUL in the path
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        line = os.fdopen(fd, "rb", closefd=False).readline(FIRST_LINE)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        record = json.loads(line)  # a line cut at the cap is not JSON
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


def _terms(fts: str | None) -> list[str]:
    """The quoted single-word terms of an FTS query, phrases left out."""
    return [e for e in (fts or "").split(" OR ") if e and " " not in e]


def _matched(conn, terms: list[str], ids: list[int]) -> Counter:
    """How many of the terms each event matches (FTS5, same tokenizer)."""
    counts = Counter()
    marks = ",".join("?" * len(ids))
    for term in terms:
        rows = conn.execute(
            "SELECT rowid FROM event_fts WHERE event_fts MATCH ?"
            f" AND rowid IN ({marks})",
            (term, *ids),
        )
        counts.update(row[0] for row in rows)
    return counts


def _knowledge_line(entry: Mapping) -> str:
    cites = ", ".join(_clean(c, 120) for c in entry["cites"])
    return (
        f"- {entry['id']} [{entry['kind']} {entry['date']}"
        f" by:{_clean(entry['actor'], 40)}]"
        f" {_clean(entry['text'], ENTRY_TEXT)}"
        + (f" (cites: {cites})" if cites else "")
    )


def _hit_line(hit: Mapping, cap: int) -> str:
    plain = hit["snippet"].replace("«", "").replace("»", "")
    head = " · ".join(
        _clean(part, 120)
        for part in (
            f"{hit['provider']} {hit['role']} {hit['kind']}",
            f"session {hit['session']}",
            hit["ts"] or "no ts",
            hit["ref"],
        )
    )
    return f"- [{head}] {_clean(plain, cap)}"


def _recall_text(entries, hits, notes) -> str:
    """The recall block within RECALL_LIMIT: knowledge, then hits, the
    last ones dropped (then the snippets shortened) until it fits."""
    tail = [*(_clean(n, 200) for n in notes), RECALL_HINT]
    for cap in (SNIPPET, 200, 120):
        items = [_knowledge_line(e) for e in entries]
        items += [_hit_line(h, cap) for h in hits]
        for count in range(len(items), 0, -1):
            lines = [classify.NOTICE, *items[:count], *tail]
            block = _frame(' kind="recall"', lines)
            if len(block) <= RECALL_LIMIT:
                return block
    return ""


def _prompt_terms(payload, trace) -> list[str] | None:
    """The query terms of the prompt, or None when it recalls nothing:
    no prompt, a slash command, or fewer than MIN_TERMS terms."""
    prompt = payload.get("prompt") if isinstance(payload, Mapping) else None
    if not isinstance(prompt, str) or prompt.lstrip().startswith("/"):
        trace["skipped"] = "prompt"
        return None
    terms = _terms(query.build_fts_query(prompt))
    trace["n_terms"] = len(terms)
    if len(terms) < MIN_TERMS:
        trace["skipped"] = "short"
        return None
    return terms


def _pushable(conn, entries: list[dict]) -> list[dict]:
    """The matched entries a hook may push without being asked (spec O5b):
    those with a live user-prompt citation, as at SessionStart."""
    ids = [int(e["id"][1:]) for e in entries]
    allowed = knowledge.user_cited(conn, ids)
    return [e for e, kid in zip(entries, ids) if kid in allowed]


def _recall_block(conn, home, payload, env, trace, terms) -> str:
    prompt = payload["prompt"]
    session = payload.get("session_id")
    entries, hits, dropped = [], [], 0
    for page in range(1, POOL_PAGES + 1):
        found = query.search(
            conn,
            prompt,
            cwd=_cwd(payload),
            env=env,
            kinds=set(EVENT_KINDS),
            limit=POOL_PAGE,
            page=page,
            current_session=session if isinstance(session, str) else None,
        )
        if "error" in found:
            break
        if page == 1:
            entries = _pushable(conn, found["knowledge"])
        counts = _matched(conn, terms, [h["id"] for h in found["hits"]])
        for hit in found["hits"]:
            if counts[hit["id"]] >= MIN_TERMS:
                hits.append(hit)
            else:
                dropped += 1
        if len(hits) >= MAX_EVENTS or not found["has_more"]:
            break
    hits = hits[:MAX_EVENTS]
    trace["returned_ids"] = [h["id"] for h in hits]
    trace["knowledge_ids"] = [int(e["id"][1:]) for e in entries]
    trace["stages"] = {"floor_dropped": dropped, "returned": len(hits)}
    if not entries and not hits:
        return ""
    return _recall_text(entries, hits, _stale(home))


def prompt_submit(
    payload: dict, provider: str, env: Mapping[str, str], *, trace=None
) -> dict:
    """UserPromptSubmit (spec 9.2): recall for the prompt, or {}.

    Matching knowledge first (user-cited entries only, as at SessionStart:
    the others stay pull-only), then at most MAX_EVENTS prompt/reply events
    of this repo, never the caller's own session, each matching at least
    MIN_TERMS (3) distinct query terms. Prompts with fewer than MIN_TERMS
    terms, slash commands and subagent transcripts recall nothing.
    """
    trace = {} if trace is None else trace
    terms = _prompt_terms(payload, trace)
    if terms is None:  # nothing to recall: the store is not even opened
        return {}

    def build(conn, home, payload, env, trace):
        return _recall_block(conn, home, payload, env, trace, terms)

    return _respond("UserPromptSubmit", payload, provider, env, trace, build)
