"""Ingest parsing: turn the new lines of one source into stored events.

Everything here runs inside the caller's write transaction; nothing commits.
A line is committed (its cursor and anchor recorded) only together with the
events it produced, which is what makes a resume after a crash exact.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import IO, TypedDict, cast

from pctx import classify, event_model, ingest_model, scope, tool_errors
from pctx.ingest_model import PROVIDER, Record, Work

__all__ = [
    "ParseResult",
    "ParseStart",
    "Progress",
    "Usage",
    "parse_source",
]

type Cursor = tuple[int, int]  # (byte offset, line number) after a line
type Anchor = tuple[int | None, str | None]  # (offset, hash) of a line
type CallPair = tuple[str, str | None]  # (originating call id, tool name)


@dataclass
class Usage:
    """Counts of pctx's own use, read at ingest and never storing text.

    Attributes:
        calls: Tool calls that invoke pctx.
        errors: Codex outputs of a pctx call that reported a failed exit.
        last_ts: Timestamp of the latest pctx call seen.
    """

    calls: int = 0
    errors: int = 0
    last_ts: str | None = None


@dataclass(frozen=True)
class Progress:
    """What a source row records about how far parsing got.

    Attributes:
        cursor: (byte offset, line number) just past the last whole line.
        anchor: (offset, hash) of the last committed line, used to detect a
            rewritten file on the next pass.
        state: JSON resume state for the next append pass, or None.
        skipped: Lines recorded as source issues.
    """

    cursor: Cursor
    anchor: Anchor
    state: str | None
    skipped: int


@dataclass(frozen=True)
class ParseStart:
    """Where parsing of a source begins.

    Attributes:
        cursor: (byte offset, line number) to resume from; (0, 0) is new.
        anchor: The previous anchor, kept if no new line is read.
        saved: The previous JSON resume state, or None.
        parent_id: Source id of the parent thread of an old-format fork.
    """

    cursor: Cursor
    anchor: Anchor
    saved: str | None
    parent_id: int | None


@dataclass(frozen=True)
class ParseResult:
    """The outcome of parsing one source.

    Attributes:
        progress: Cursor, anchor, state and skipped count to commit.
        added: Events inserted.
        usage: pctx usage counts found in the new lines.
    """

    progress: Progress
    added: int
    usage: Usage


class _SavedState(TypedDict, total=False):
    """The JSON that `_dump_state` writes and `_load_state` reads back."""

    cwd: str | None
    calls: dict[str, list[str | None]]
    cells: dict[str, list[str | None]]
    prefix_open: bool
    pctx: list[str]
    pending: dict[str, int]


@dataclass
class _Extra:
    """Resume fields that belong to ingest, not to classify."""

    prefix_open: bool
    pctx: set[str]  # call ids of pctx calls still awaiting their output


def parse_source(
    conn: sqlite3.Connection,
    w: Work,
    source_id: int,
    start: ParseStart,
    scopes: dict[str | None, int],
) -> ParseResult:
    """Classify and insert the new lines of one source.

    Args:
        conn: Connection inside the source's open write transaction.
        w: The source being written.
        source_id: Its source row id.
        start: Where to begin reading.
        scopes: Cache of cwd to scope id, shared across sources of a pass.

    Returns:
        The progress to commit and the counts gathered.
    """
    return _SourceParser(conn, w, source_id, start, scopes).run()


class _SourceParser:
    """Parses one source's new lines; holds the per-source mutable state."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        w: Work,
        source_id: int,
        start: ParseStart,
        scopes: dict[str | None, int],
    ) -> None:
        """Load resume state, line tombstones and the fork-prefix hashes."""
        self.conn = conn
        self.w = w
        self.source_id = source_id
        self.start = start
        self.scopes = scopes
        self.provider = PROVIDER[w.name]
        self.state, self.pending, self.extra = _load_state(start.saved, w.info)
        self.events_of = (
            classify.codex_events
            if self.provider == "codex"
            else classify.claude_events
        )
        self.tombs = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT line, line_sha256 FROM tombstone WHERE provider = ?"
                " AND level = 'line' AND thread_id = ?",
                (self.provider, w.info.thread_id),
            )
        }
        self.prefix = (
            _event_hashes(conn, start.parent_id)
            if self.extra.prefix_open
            else set[bytes]()
        )
        # An old fork whose parent has no events has no prefix to skip.
        self.extra.prefix_open = bool(self.prefix) and self.extra.prefix_open
        self.added = 0
        self.skipped = 0
        self.usage = Usage()
        self.cursor = start.cursor
        self.anchor = start.anchor

    def run(self) -> ParseResult:
        """Read every whole line from the start cursor to the end of file."""
        with self.w.path.open("rb") as handle:
            handle.seek(self.start.cursor[0])
            for number, begin, end, raw in _lines(handle, *self.start.cursor):
                # Advance the cursor for every whole line, usable or not, so
                # a bad line is skipped once and never re-read.
                self.cursor = (end, number)
                self._line(number, begin, raw)
        progress = Progress(
            self.cursor,
            self.anchor,
            _dump_state(self.state, self.pending, self.extra),
            self.skipped,
        )
        return ParseResult(progress, self.added, self.usage)

    def _line(self, number: int, begin: int, raw: bytes | None) -> None:
        """Record one line as an issue, skip it, or store its events."""
        if raw is None:
            self._issue(number, "line_too_large")
            return
        digest = classify.record_hash(raw)
        self.anchor = (begin, digest)
        if (number, digest) in self.tombs:
            return  # an erased line never re-enters
        record = _decode(raw)
        if isinstance(record, str):
            self._issue(number, record)
            return
        found = self.events_of(record, number, self.state)
        if self.provider == "codex":
            _count_output(record, self.extra, self.usage)
        cwd = classify.cwd_of(record, self.state)
        for ev in found:
            if self.extra.prefix_open:
                if _role_text(ev.role, ev.text) in self.prefix:
                    continue  # the fork's copy of its parent's history
                self.extra.prefix_open = False
            event_id = self._insert(ev, cwd, begin, digest)
            self.added += 1
            if ev.kind == "tool_call":
                self._note_call(ev, event_id)

    def _issue(self, number: int, code: str) -> None:
        """Record a code (never the line's text) for an unusable line."""
        self.conn.execute(
            "INSERT OR REPLACE INTO source_issue(source_id, line, at,"
            " code) VALUES (?, ?, ?, ?)",
            (self.source_id, number, time.time(), code),
        )
        self.skipped += 1

    def _insert(
        self, ev: classify.EventRec, cwd: str | None, begin: int, digest: str
    ) -> int:
        """Insert one event and return its id."""
        if cwd not in self.scopes:
            # The commit hint lets a deleted cwd resolve to its repo.
            self.scopes[cwd] = scope.scope_id(
                self.conn, cwd or "", self.w.info.commit_hash
            )
        parent = None
        if ev.kind == "tool_error" and ev.call_id:
            parent = self.pending.get(ev.call_id)
        new_id = self.conn.execute(
            "INSERT INTO event(source_id, line, part, byte_offset,"
            " line_sha256, seq, ts, role, kind, tag, scope_id, cwd,"
            " parent_event_id, flags, text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.source_id,
                ev.line,
                ev.part,
                begin,
                digest,
                ev.seq,
                ev.ts,
                ev.role,
                ev.kind,
                ev.tag,
                self.scopes[cwd],
                cwd,
                parent,
                ev.flags,
                ev.text,
            ),
        ).lastrowid
        # An INSERT that did not raise always sets lastrowid; typeshed types
        # it as Optional because it is None after other statement kinds.
        return cast(int, new_id)

    def _note_call(self, ev: classify.EventRec, event_id: int) -> None:
        """Remember a tool_call for linking and count calls that run pctx."""
        if ev.call_id:
            self.pending[ev.call_id] = event_id
        # ponytail: the pctx test runs on the 4 KiB-capped text; a call whose
        # pctx word sits later is not counted.
        if ev.flags & classify.FLAG_MARKER and classify.PCTX_CALL.search(
            ev.text
        ):
            self.usage.calls += 1
            self.usage.last_ts = (
                max(self.usage.last_ts or "", ev.ts or "") or None
            )
            if self.provider == "codex" and ev.call_id:
                self.extra.pctx.add(ev.call_id)


def _lines(
    handle: IO[bytes], offset: int, number: int
) -> Iterator[tuple[int, int, int, bytes | None]]:
    """Yield (line number, start, end, raw) of each whole line.

    ``raw`` is None for a line over MAX_LINE_BYTES.  Iteration stops before
    a partial tail: a line still being written is read once its newline
    exists, so the cursor never lands mid-line.
    """
    limit = ingest_model.MAX_LINE_BYTES
    while True:
        raw = handle.readline(limit + 1)
        if not raw.endswith(b"\n"):
            if len(raw) <= limit:
                return  # EOF, or a line still being written
            # Oversize: drain it in 1 MiB reads so it is never held whole.
            size = len(raw)
            while not raw.endswith(b"\n"):
                raw = handle.readline(1 << 20)
                if not raw:
                    return  # an oversize line still being written
                size += len(raw)
            number += 1
            yield number, offset, offset + size, None
            offset += size
            continue
        number += 1
        yield number, offset, offset + len(raw), raw
        offset += len(raw)


def _decode(raw: bytes) -> Record | str:
    """Return the JSON object on a line, or the issue code that rejects it."""
    try:
        record = json.loads(raw)
    except RecursionError:
        return "too_deep"
    except ValueError:  # includes invalid UTF-8
        return "invalid_json"
    # The depth check comes before the type check so a hostile deeply nested
    # array is reported as too_deep, not as not_object.
    if not classify.within_depth(record):
        return "too_deep"
    obj = _as_record(record)
    return "not_object" if obj is None else obj


def _as_record(value: object) -> Record | None:
    """Return ``value`` if it is a JSON object, else None."""
    if isinstance(value, dict):
        # isinstance leaves the types unknown; JSON object keys are strings.
        return cast(Record, value)
    return None


def _count_output(record: Record, extra: _Extra, usage: Usage) -> None:
    """Count a failed exit of a pctx call's output; the output is not kept.

    classify skips the output text itself (its call invokes pctx), so the
    exit status is read here and only the count survives.
    """
    payload = _as_record(record.get("payload"))
    if record.get("type") != "response_item" or payload is None:
        return
    call_id = payload.get("call_id")
    if call_id not in extra.pctx or payload.get("type") not in (
        "function_call_output",
        "custom_tool_call_output",
    ):
        return
    extra.pctx.discard(call_id)
    output = payload.get("output")
    # ponytail: an exec cell still running reports later through wait();
    # only its first output is read here.
    text = output if isinstance(output, str) else event_model.join_text(output)
    exits = tool_errors.EXIT_CODE.findall(text)
    if text.startswith("Script failed") or any(
        int(a or b) != 0 for a, b in exits
    ):
        usage.errors += 1


def _role_text(role: str, text: str) -> bytes:
    """Return sha256(role, text), the identity of a copied history entry.

    The content-prefix rule compares a fork's early events with its
    parent's by this hash.  ``surrogatepass`` keeps lone surrogates from a
    lossy transcript hashable instead of raising.
    """
    data = f"{role}\n{text}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).digest()


def _event_hashes(
    conn: sqlite3.Connection, source_id: int | None
) -> set[bytes]:
    """Return the `_role_text` hash of every event of a source."""
    if source_id is None:
        return set()
    rows = conn.execute(
        "SELECT role, text FROM event WHERE source_id = ?", (source_id,)
    )
    return {_role_text(role, text) for role, text in rows}


def _pairs(raw: Mapping[str, list[str | None]]) -> dict[str, CallPair]:
    """Rebuild classify's (origin, tool) pairs from their JSON arrays."""
    # JSON has no tuple type: _dump_state wrote each pair as a 2-element
    # array, which classify expects back as a tuple.
    return {k: cast(CallPair, tuple(v)) for k, v in raw.items()}


def _load_state(
    saved: str | None, info: classify.ThreadInfo
) -> tuple[classify.CodexState, dict[str, int], _Extra]:
    """Rebuild the classify state, the pending-call map and ingest's own.

    Returns:
        (classify state, call id to event id of unlinked tool calls, extra
        resume fields).
    """
    data: _SavedState = json.loads(saved) if saved else {}
    state = classify.CodexState(
        cwd=data.get("cwd"),
        replay_before=info.replay_before,
        thread_class=info.thread_class,
        calls=_pairs(data.get("calls", {})),
        cells=_pairs(data.get("cells", {})),
    )
    extra = _Extra(
        prefix_open=data.get("prefix_open", True),
        pctx=set(data.get("pctx", ())),
    )
    return state, data.get("pending", {}), extra


def _dump_state(
    state: classify.CodexState, pending: dict[str, int], extra: _Extra
) -> str:
    """Return the JSON a later append pass needs to resume this source.

    Linked entries are pruned: a call id stays only while classify can
    still route an output to it, which bounds the state's growth on a long
    transcript.
    """
    live = {origin for origin, _ in state.calls.values()}
    live |= {origin for origin, _ in state.cells.values()}
    return json.dumps(
        {
            "cwd": state.cwd,
            "thread_class": state.thread_class,
            "replay_before": state.replay_before,
            "calls": state.calls,
            "cells": state.cells,
            "pending": {c: e for c, e in pending.items() if c in live},
            "prefix_open": extra.prefix_open,
            "pctx": sorted(extra.pctx & set(state.calls)),
        },
        sort_keys=True,
    )
