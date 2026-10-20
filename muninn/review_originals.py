"""Read bounded physical provider originals without changing their contents."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing
from io import BufferedReader
from pathlib import Path
from typing import Any, BinaryIO, cast

from muninn import ingest
from muninn.event_model import as_record
from muninn.platform_io import open_regular
from muninn.review_evidence_models import (
    CursorOriginal,
    JsonlOriginal,
    OriginalLocator,
)
from muninn.review_read_budget import ReadBudget
from muninn.review_selectors import (
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    MAX_SCAN_BYTES,
    cursor_materialized,
    decode,
)


def _lines(
    handle: BinaryIO, max_bytes: int, *, budget: ReadBudget
) -> Iterator[bytes]:
    """Yield complete records while bounding underlying buffered reads."""
    remaining = min(max_bytes, MAX_SCAN_BYTES, budget.bytes_left)
    pending = b""
    while remaining > 0:
        if budget.records_left == 0:
            raise ValueError("evidence_limit")
        # read1 performs at most one bounded raw read, unlike readline's
        # implicit buffer refill which may exceed a small caller budget.
        block = cast(BufferedReader, handle).read1(
            min(8192, remaining, MAX_RECORD_BYTES + 1 - len(pending))
        )
        budget.consume_bytes(len(block))
        remaining -= len(block)
        pending += block
        while b"\n" in pending:
            budget.consume_record()
            raw, pending = pending.split(b"\n", 1)
            if len(raw) + 1 > MAX_RECORD_BYTES:
                raise ValueError("original_unavailable")
            yield raw + b"\n"
        if not block or len(pending) >= MAX_RECORD_BYTES:
            raise ValueError("original_unavailable")
    raise ValueError("evidence_limit")


def _jsonl(
    source: sqlite3.Row,
    first: int,
    last: int,
    max_bytes: int = MAX_SCAN_BYTES,
    *,
    budget: ReadBudget,
) -> list[tuple[int, bytes, OriginalLocator]]:
    """Establish physical coordinates from a bounded scan of the origin."""
    root = ingest.default_roots().get(source["root"])
    if root is None:
        raise ValueError("original_unavailable")
    # Index offsets cannot prove current line numbers after prefix rewrites.
    number, offset = 1, 0
    path = root / source["path"]
    output: list[tuple[int, bytes, OriginalLocator]] = []
    with open_regular(path, root=root) as handle:
        handle.seek(offset)
        for count, raw in enumerate(_lines(handle, max_bytes, budget=budget)):
            if count == MAX_RECORDS:
                raise ValueError("evidence_limit")
            if number >= first:
                output.append(
                    (
                        number,
                        raw,
                        JsonlOriginal(
                            kind="jsonl",
                            path=path.absolute(),
                            byte_offset=offset,
                            byte_length=len(raw),
                        ),
                    )
                )
            if number == last:
                return output
            offset += len(raw)
            number += 1
    raise ValueError("evidence_limit")


def _execute(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
    *,
    budget: ReadBudget,
) -> sqlite3.Cursor:
    """Refuse new native statements after the shared VM grant expires."""
    if budget.sql_steps_left == 0:
        raise ValueError("evidence_limit")
    return conn.execute(sql, parameters)


def _cursor_connection(
    stack: ExitStack, path: Path, *, budget: ReadBudget
) -> sqlite3.Connection:
    """Hold a read-only snapshot charged to the operation until closure."""
    if budget.sql_steps_left == 0:
        raise ValueError("evidence_limit")
    conn = stack.enter_context(
        closing(
            sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
        )
    )
    conn.set_progress_handler(budget.sqlite_progress, 1)
    stack.callback(conn.set_progress_handler, None, 0)
    _execute(conn, "PRAGMA query_only=ON", budget=budget)
    _execute(conn, "BEGIN", budget=budget)
    # Key guards cast TEXT to database-encoded BLOBs, but returned keys
    # consume UTF-8 bytes. Refuse other encodings before reading originals.
    if (
        _execute(conn, "PRAGMA encoding", budget=budget).fetchone()[0]
        != "UTF-8"
    ):
        raise ValueError("original_unavailable")
    return conn


def _cell(
    conn: sqlite3.Connection, key: str, limit: int, *, budget: ReadBudget
) -> bytes:
    """Fetch exactly one bounded original cell within the byte limit."""
    budget.consume_record()
    limit = min(limit, budget.bytes_left, MAX_RECORD_BYTES)
    count = _execute(
        conn,
        "SELECT count(*) FROM (SELECT 1 FROM cursorDiskKV "
        "WHERE key=? LIMIT 2)",
        (key,),
        budget=budget,
    ).fetchone()[0]
    if count != 1:
        raise ValueError("original_unavailable")
    row = _execute(
        conn,
        "SELECT CASE WHEN length(CAST(value AS BLOB))<=? THEN CAST(value AS "
        "BLOB) END FROM cursorDiskKV WHERE key=? LIMIT 1",
        (limit, key),
        budget=budget,
    ).fetchone()
    if row is None:
        raise ValueError("original_unavailable")
    if not isinstance(row[0], bytes):
        raise ValueError("evidence_limit")
    budget.consume_bytes(len(row[0]))
    return row[0]


def _keys(
    conn: sqlite3.Connection, prefix: str, limit: int, *, budget: ReadBudget
) -> tuple[list[str], int]:
    """Charge native key rows before processing their bounded contents."""
    keys: list[str] = []
    rowid: int | None = None
    while True:
        if budget.records_left == 0:
            raise ValueError("evidence_limit")
        row = _execute(
            conn,
            "SELECT rowid,CASE WHEN length(CAST(key AS BLOB))<=? "
            "THEN key END FROM cursorDiskKV WHERE key>=? AND key<? "
            "AND (? IS NULL OR rowid>?) ORDER BY rowid LIMIT 1",
            (
                min(limit, budget.bytes_left, 8192),
                prefix,
                f"{prefix[:-1]};",
                rowid,
                rowid,
            ),
            budget=budget,
        ).fetchone()
        if row is None:
            return keys, limit
        budget.consume_record()
        rowid, key = row
        if isinstance(key, str):
            size = len(key.encode("utf-8"))
            budget.consume_bytes(size)
            limit -= size
        if not isinstance(key, str) or key in keys or len(keys) == MAX_RECORDS:
            raise ValueError("evidence_limit")
        keys.append(key)


def _cursor(
    source: sqlite3.Row,
    first: int,
    last: int,
    max_bytes: int,
    *,
    budget: ReadBudget,
) -> list[tuple[int, bytes, OriginalLocator]]:
    """Reproduce imported visible-bubble order within bounded native reads."""
    database, separator, thread = source["path"].rpartition("#")
    if not separator or thread != source["thread_id"]:
        raise ValueError("original_unavailable")
    path = Path(database)
    output: list[tuple[int, bytes, OriginalLocator]] = []
    with ExitStack() as stack:
        conn = _cursor_connection(stack, path, budget=budget)
        remaining = min(max_bytes, MAX_SCAN_BYTES)
        raw = _cell(conn, f"composerData:{thread}", remaining, budget=budget)
        remaining -= len(raw)
        composer = decode(raw)
        if composer.get("composerId", thread) != thread:
            raise ValueError("original_unavailable")
        prefix = f"bubbleId:{thread}:"
        keys, remaining = _keys(conn, prefix, remaining, budget=budget)
        headers = composer.get("fullConversationHeadersOnly")
        if isinstance(headers, list):
            keys = list(
                dict.fromkeys(
                    prefix + h["bubbleId"]
                    for item in cast(list[Any], headers)
                    if (h := as_record(item)) is not None
                    and isinstance(h.get("bubbleId"), str)
                )
            )
        if len(keys) > MAX_RECORDS:
            raise ValueError("evidence_limit")
        number = 0
        for key in keys:
            raw = _cell(conn, key, remaining, budget=budget)
            remaining -= len(raw)
            record = decode(raw)
            if not cursor_materialized(record):
                continue
            number += 1
            if first <= number <= last:
                output.append(
                    (
                        number,
                        raw,
                        CursorOriginal(
                            kind="cursor_db",
                            database=path,
                            conversation_id=thread,
                            record_key=key,
                        ),
                    )
                )
            if number == last:
                return output
    raise ValueError("original_unavailable")


def originals(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    first: int,
    last: int,
    max_bytes: int = MAX_SCAN_BYTES,
    *,
    require_cursor: bool = True,
    budget: ReadBudget | None = None,
) -> list[tuple[int, bytes, OriginalLocator]]:
    """Read only bounded admitted coordinates and normalize read failures."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("evidence_limit")
    budget = budget if budget is not None else ReadBudget(max_bytes)
    if last - first >= MAX_RECORDS:
        raise ValueError("evidence_limit")
    if (
        require_cursor
        and source["provider"] != "cursor"
        and last > source["cursor_line"]
    ):
        raise ValueError("original_unavailable")
    try:
        return (
            _cursor(source, first, last, max_bytes, budget=budget)
            if source["provider"] == "cursor"
            else _jsonl(source, first, last, max_bytes, budget=budget)
        )
    except (OSError, sqlite3.Error) as exc:
        code = (
            "evidence_limit"
            if isinstance(exc, sqlite3.Error)
            and exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
            and budget.sql_steps_left == 0
            else "original_unavailable"
        )
        raise ValueError(code) from exc


def reread(
    stack: ExitStack,
    connections: dict[Path, sqlite3.Connection],
    original: OriginalLocator,
    limit: int,
    *,
    budget: ReadBudget,
) -> bytes:
    """Read an exact stored locator without scanning transcripts at commit."""
    try:
        if isinstance(original, CursorOriginal):
            conn = connections.get(original.database)
            if conn is None:
                conn = _cursor_connection(
                    stack, original.database, budget=budget
                )
                connections[original.database] = conn
            return _cell(
                conn,
                original.record_key,
                min(limit, MAX_RECORD_BYTES),
                budget=budget,
            )
        if original.byte_length > min(
            limit, MAX_RECORD_BYTES, budget.bytes_left
        ):
            raise ValueError("evidence_limit")
        budget.consume_record()
        with open_regular(original.path) as handle:
            handle.seek(original.byte_offset)
            raw = b""
            while len(raw) < original.byte_length:
                block = cast(BufferedReader, handle).read1(
                    min(
                        8192,
                        original.byte_length - len(raw),
                        budget.bytes_left,
                    )
                )
                budget.consume_bytes(len(block))
                if not block:
                    break
                raw += block
        if len(raw) != original.byte_length or not raw.endswith(b"\n"):
            raise ValueError("original_unavailable")
        return raw
    except (OSError, sqlite3.Error) as exc:
        code = (
            "evidence_limit"
            if isinstance(exc, sqlite3.Error)
            and exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
            and budget.sql_steps_left == 0
            else "original_unavailable"
        )
        raise ValueError(code) from exc
