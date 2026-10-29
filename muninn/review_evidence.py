"""Read bounded original evidence and revalidate stored physical provenance."""

from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

from muninn import classify, ingest, store
from muninn.knowledge_models import EvidenceId, Scope
from muninn.knowledge_scope import scope_applies
from muninn.review_evidence_models import (
    CursorOriginal,
    EvidenceLocator,
    EvidenceRecord,
    EvidenceSelector,
    EvidenceWindow,
    InitialEvidence,
    OriginalRecord,
    OutcomeLocator,
    SourceRef,
)
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_originals import originals, reread
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from muninn.review_selectors import (
    MAX_RECORDS,
    MAX_SCAN_BYTES,
    PROJECTION_VERSION,
    build_record,
    check_tombstones,
    decode,
    fields,
)


def _source(conn: sqlite3.Connection, source: SourceRef) -> sqlite3.Row:
    """Require an indexed active primary original source."""
    row = conn.execute(
        "SELECT * FROM source WHERE provider=? AND thread_id=?",
        (source.provider, source.thread_id),
    ).fetchone()
    if (
        row is None
        or row["status"] != "active"
        or row["thread_class"] != "primary"
    ):
        raise ValueError("source_unavailable")
    return row


def validate_window_boundary(
    conn: sqlite3.Connection,
    window: EvidenceWindow,
    max_bytes: int,
    *,
    budget: ReadBudget | None = None,
    selected_line: int | None = None,
) -> OriginalRecord:
    """Check a complete physical boundary within the caller's read budget.

    Args:
        conn: Caller-owned connection and transaction.
        window: Inclusive physical boundary and original hash.
        max_bytes: Positive cap on all original and traversal bytes read.
        budget: Shared operation allowance, or a new standalone account.
        selected_line: Optional admitted line to retain from the boundary scan.

    Returns:
        The selected original, or the boundary original when none is selected.

    Raises:
        ValueError: If the boundary is changed, erased or unavailable, or
            the read exceeds its byte or structural limits.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("evidence_limit")
    source = _source(conn, window.source)
    locator = EvidenceLocator(
        source=window.source,
        line=window.through_line,
        part=1,
        line_sha256=window.boundary_hash,
    )
    check_tombstones(conn, source, locator)
    return validate_cursor_boundary(
        conn, window, max_bytes, budget=budget, selected_line=selected_line
    )


def validate_cursor_boundary(
    conn: sqlite3.Connection,
    window: EvidenceWindow,
    max_bytes: int,
    *,
    budget: ReadBudget | None = None,
    selected_line: int | None = None,
) -> OriginalRecord:
    """Check an old cursor hash without admitting its record as evidence."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("evidence_limit")
    source = _source(conn, window.source)
    first = window.through_line if selected_line is None else selected_line
    if not window.from_line <= first <= window.through_line:
        raise ValueError("rejected_scope")
    records = originals(
        conn,
        source,
        first,
        window.through_line,
        max_bytes,
        require_cursor=False,
        budget=budget,
    )
    if classify.record_hash(records[-1][1]) != window.boundary_hash:
        raise ValueError("evidence_changed")
    return records[0]


def select_evidence(
    home: Path, locator: EvidenceLocator, selector: EvidenceSelector
) -> EvidenceRecord:
    """Select a permitted original field; the caller checks admission."""
    with closing(store.connect_ro(store.db_path(home))) as conn:
        source = _source(conn, locator.source)
        _, raw, original = originals(conn, source, locator.line, locator.line)[
            0
        ]
        return build_record(conn, source, locator, selector, raw, original)


def initial_evidence(
    home: Path, window: EvidenceWindow, max_bytes: int
) -> InitialEvidence:
    """Bound excerpts independently from the structural original-read cap."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("evidence_limit")
    excerpts: list[EvidenceRecord] = []
    outcomes: list[OutcomeLocator] = []
    used, truncated = 0, False
    with closing(store.connect_ro(store.db_path(home))) as conn:
        source = _source(conn, window.source)
        records = originals(
            conn,
            source,
            window.from_line,
            window.through_line,
            budget=ReadBudget(MAX_SCAN_BYTES),
        )
        if classify.record_hash(records[-1][1]) != window.boundary_hash:
            raise ValueError("evidence_changed")
        seen: set[tuple[int, int]] = set()
        for number, raw, original in records:
            for field in fields(window.source.provider, decode(raw)):
                if not field.text.strip() and field.evidence_type in (
                    "owner_statement",
                    "assistant_report",
                ):
                    continue
                locator = EvidenceLocator(
                    source=window.source,
                    line=number,
                    part=field.part,
                    line_sha256=classify.record_hash(raw),
                )
                check_tombstones(conn, source, locator)
                if field.evidence_type in ("tool_result", "tool_request"):
                    if (number, field.part) in seen:
                        continue
                    seen.add((number, field.part))
                    value = OutcomeLocator(
                        locator=locator,
                        evidence_type=field.evidence_type,
                        status_code=field.status,
                    )
                else:
                    selector = EvidenceSelector(
                        json_pointer=field.pointer, start=None, end=None
                    )
                    value = build_record(
                        conn, source, locator, selector, raw, original
                    )
                size = len(canonical_json(value).encode())
                if used + size > max_bytes:
                    truncated = True
                    continue
                used += size
                if isinstance(value, OutcomeLocator):
                    outcomes.append(value)
                else:
                    excerpts.append(value)
    return InitialEvidence(
        excerpts=tuple(excerpts),
        outcomes=tuple(outcomes),
        truncated=truncated,
        bytes_used=used,
    )


def _stored(
    conn: sqlite3.Connection, evidence_id: EvidenceId, limit: int
) -> tuple[EvidenceRecord, int]:
    """Load one bounded durable record through the strict data contract."""
    row = conn.execute(
        "SELECT * FROM review_evidence WHERE id=? AND "
        "length(CAST(coalesce(excerpt,'') AS BLOB))+"
        "length(CAST(original_locator_json AS BLOB))+"
        "length(CAST(selector_json AS BLOB))+"
        "length(CAST(scope_json AS BLOB))<=?",
        (evidence_id, limit),
    ).fetchone()
    if row is None:
        if conn.execute(
            "SELECT 1 FROM review_evidence WHERE id=?", (evidence_id,)
        ).fetchone():
            raise EvidenceLimitError("evidence_selection_limit")
        raise ValueError("evidence_unavailable")
    data: dict[str, Any] = dict(row)
    size = sum(len(v.encode()) for v in data.values() if isinstance(v, str))
    if size > limit:
        raise EvidenceLimitError("evidence_selection_limit")
    for key in ("scope", "selector", "original_locator"):
        data[key] = decode(data.pop(f"{key}_json").encode())
    return (
        EvidenceRecord.model_validate_json(json.dumps(data), strict=True),
        size,
    )


def _check_physical(source: sqlite3.Row, record: EvidenceRecord) -> None:
    """Bind stored physical provenance to the authoritative source path."""
    original = record.original_locator
    if isinstance(original, CursorOriginal):
        if (
            source["path"] != f"{original.database}#{original.conversation_id}"
            or original.conversation_id != record.thread_id
            or not original.record_key.startswith(
                f"bubbleId:{record.thread_id}:"
            )
        ):
            raise ValueError("evidence_changed")
    else:
        root = ingest.default_roots().get(source["root"])
        if root is None or (root / source["path"]).absolute() != original.path:
            raise ValueError("evidence_changed")
        original.path.resolve().relative_to(root.resolve())


def validate_evidence(
    conn: sqlite3.Connection,
    home: Path,
    evidence_ids: tuple[EvidenceId, ...],
    scope: Scope,
    policy: ReviewPolicy,
    *,
    budget: ReadBudget | None = None,
) -> tuple[EvidenceRecord, ...]:
    """Revalidate bounded persisted selections in the caller transaction."""
    if len(evidence_ids) > MAX_RECORDS or len(set(evidence_ids)) != len(
        evidence_ids
    ):
        raise ValueError("evidence_limit")
    if policy.projection_version != PROJECTION_VERSION or (
        scope.project_id is None and not policy.allow_global
    ):
        raise ValueError("evidence_policy")
    budget = (
        budget if budget is not None else ReadBudget(policy.max_input_bytes)
    )
    result: list[EvidenceRecord] = []
    remaining = min(policy.max_input_bytes, MAX_SCAN_BYTES)
    selected_remaining = remaining
    fresh_originals: dict[str, bytes] = {}
    connections: dict[Path, sqlite3.Connection] = {}
    with ExitStack() as stack:
        for evidence_id in evidence_ids:
            record, size = _stored(conn, evidence_id, selected_remaining)
            selected_remaining -= size
            if (
                record.state != "live"
                or not scope_applies(record.scope, scope)
                or record.projection_version != policy.projection_version
            ):
                raise ValueError("evidence_unavailable")
            locator = EvidenceLocator(
                source=SourceRef(
                    provider=record.provider, thread_id=record.thread_id
                ),
                line=record.line,
                part=record.part,
                line_sha256=record.line_sha256,
            )
            source = _source(conn, locator.source)
            _check_physical(source, record)
            key = canonical_json(record.original_locator)
            raw = fresh_originals.get(key)
            if raw is None:
                raw = reread(
                    stack,
                    connections,
                    record.original_locator,
                    remaining,
                    budget=budget,
                )
                remaining -= len(raw)
                fresh_originals[key] = raw
            fresh = build_record(
                conn,
                source,
                locator,
                record.selector,
                raw,
                record.original_locator,
            )
            if fresh.model_dump(
                exclude={"id", "selected_at"}
            ) != record.model_dump(exclude={"id", "selected_at"}):
                raise ValueError("evidence_changed")
            result.append(record)
    return tuple(result)
