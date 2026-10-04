"""Ingest line reading: split a transcript into lines and decode them.

A line is yielded only once its newline exists, so a cursor never lands
mid-line, and a line over the size limit is skipped without being held.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import IO, cast

from muninn import classify, ingest_model
from muninn.ingest_model import Record

__all__ = ["as_record", "decode", "lines"]


def lines(
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


def decode(raw: bytes) -> Record | str:
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
    obj = as_record(record)
    return "not_object" if obj is None else obj


def as_record(value: object) -> Record | None:
    """Return ``value`` if it is a JSON object, else None."""
    if isinstance(value, dict):
        # isinstance leaves the types unknown; JSON object keys are strings.
        return cast(Record, value)
    return None
