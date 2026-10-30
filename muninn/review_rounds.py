"""Define and locally validate bounded reviewer JSON operations."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError

from muninn import knowledge_push, knowledge_read, store
from muninn.knowledge_models import (
    KnowledgeId,
    ReviewResult,
    Revision,
    Scope,
    StrictModel,
    Text,
)
from muninn.review_correction import RoundFormatError
from muninn.review_evidence import validate_evidence
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceSelector,
    OriginalRecord,
)
from muninn.review_json import (
    canonical_json,
    finite_float,
    reject_constant,
    unique_object,
)
from muninn.review_models import ReviewContext, ReviewPolicy
from muninn.review_progress import check_window, digest
from muninn.review_read_budget import ReadBudget
from muninn.review_request_views import request_row, request_view
from muninn.review_selectors import build_record


class SearchRequest(StrictModel):
    """Request a bounded knowledge search in an admitted scope."""

    op: Literal["search"]
    query: Text
    scope: Scope


class OpenRequest(StrictModel):
    """Request a governed knowledge identity and optional revision."""

    op: Literal["open"]
    knowledge_id: KnowledgeId
    revision: Revision | None


class EvidenceRequest(StrictModel):
    """Select an exact source coordinate without granting read authority."""

    op: Literal["evidence"]
    locator: EvidenceLocator
    selector: EvidenceSelector


class FinalRequest(StrictModel):
    """Submit locally validated proposals for separate atomic admission."""

    op: Literal["final"]
    result: ReviewResult


type RoundRequest = Annotated[
    SearchRequest | OpenRequest | EvidenceRequest | FinalRequest,
    Field(discriminator="op"),
]
_ADAPTER = TypeAdapter[RoundRequest](RoundRequest)


def round_schema() -> dict[str, object]:
    """Generate the CLI schema from the locally validated union."""
    return _ADAPTER.json_schema()


def check_round(context: ReviewContext) -> None:
    """Refuse exhausted local round and monotonic attempt boundaries."""
    if context.round_index > context.policy.max_rounds:
        raise ValueError("budget_round_limit")
    if time.monotonic() >= context.deadline:
        raise ValueError("budget_deadline")


def _scope(request: RoundRequest, context: ReviewContext) -> None:
    """Reject widened search or locator windows before any later read."""
    if isinstance(request, SearchRequest) and (
        request.scope != context.scope
        and not (
            context.policy.allow_global
            and request.scope.project_id is None
            and request.scope.task is None
        )
    ):
        raise ValueError("rejected_scope")
    if isinstance(request, EvidenceRequest) and not any(
        request.locator.source == window.source
        and window.from_line <= request.locator.line <= window.through_line
        for window in context.admitted_windows
    ):
        raise ValueError("rejected_scope")


def parse_round(raw: bytes, context: ReviewContext) -> RoundRequest:
    """Validate one bounded UTF-8 operation without reading any evidence."""
    try:
        context = ReviewContext.model_validate_json(context.model_dump_json())
        check_round(context)
        if len(raw) > context.policy.max_output_bytes:
            raise ValueError("invalid_round_size")
        try:
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
                parse_float=finite_float,
            )
        except (ValueError, RecursionError):
            raise ValueError("invalid_round") from None
    except (ValidationError, json.JSONDecodeError, UnicodeError):
        raise ValueError("invalid_round") from None
    try:
        request = _ADAPTER.validate_json(raw, strict=True)
    except ValidationError as error:
        if any(
            item["type"] == "json_invalid"
            for item in error.errors(include_input=False, include_url=False)
        ):
            raise ValueError("invalid_round") from None
        raise RoundFormatError() from None
    _scope(request, context)
    return request


def _current(
    conn: sqlite3.Connection,
    context: ReviewContext,
    budget: ReadBudget,
    selected_line: int | None,
) -> tuple[sqlite3.Row, OriginalRecord]:
    """Fence mutable attempts and prove the durable request boundary."""
    row = request_row(conn, context.request_id)
    attempt = conn.execute(
        "SELECT * FROM review_attempt WHERE id=?", (context.attempt_id,)
    ).fetchone()
    control = conn.execute("SELECT * FROM review_control").fetchone()
    if (
        row["state"] != "running"
        or row["current_attempt_id"] != context.attempt_id
        or attempt is None
        or attempt["request_id"] != context.request_id
        or attempt["ended_at"] is not None
        or attempt["generation"] != context.generation
        or control["generation"] != context.generation
    ):
        raise ValueError("rejected_fenced")
    policy = canonical_json(context.policy)
    if (
        not control["admission"]
        or row["policy_json"] != policy
        or control["policy_json"] is None
        or canonical_json(
            ReviewPolicy.model_validate_json(control["policy_json"])
        )
        != policy
        or row["policy_digest"] != digest(context.policy)
        or control["policy_digest"] != digest(context.policy)
    ):
        raise ValueError("review_policy_required")
    view = request_view(row)
    if context.scope != view.scope or context.admitted_windows != (
        view.window,
    ):
        raise ValueError("rejected_scope")
    ids = tuple(
        row[0]
        for row in conn.execute(
            "SELECT evidence_id FROM review_input WHERE request_id=?",
            (context.request_id,),
        )
    )
    if set(ids) != set(context.evidence_ids) or len(ids) != len(
        context.evidence_ids
    ):
        raise ValueError("rejected_evidence")
    source, original = check_window(
        conn,
        view.window,
        view.scope,
        context.policy,
        budget=budget,
        selected_line=selected_line,
    )
    if source["session_root"] != row["session_root"]:
        raise ValueError("evidence_changed")
    return source, original


def _link(
    conn: sqlite3.Connection, context: ReviewContext, ids: tuple[str, ...]
) -> None:
    """Persist program-resolved support before any returned content."""
    conn.executemany(
        "INSERT OR IGNORE INTO review_input VALUES(?,?)",
        [(context.request_id, ident) for ident in ids],
    )


def _selection(
    conn: sqlite3.Connection,
    request: EvidenceRequest,
    source: sqlite3.Row,
    selected: OriginalRecord,
) -> str:
    """Persist a selection from this request's exact admitted original."""
    locator = request.locator
    _, raw, original = selected
    record = build_record(
        conn, source, locator, request.selector, raw, original
    )
    key = (
        record.provider,
        record.thread_id,
        record.line,
        record.part,
        record.line_sha256,
        record.projection_version,
        record.selector_digest,
    )
    existing = conn.execute(
        "SELECT id FROM review_evidence WHERE provider=? AND thread_id=? "
        "AND line=? AND part=? AND line_sha256=? AND projection_version=? "
        "AND selector_digest=?",
        key,
    ).fetchone()
    if existing is not None:
        return existing[0]
    data = record.model_dump(mode="json")
    for field in ("scope", "selector", "original_locator"):
        data[f"{field}_json"] = json.dumps(data.pop(field))
    conn.execute(
        f"INSERT INTO review_evidence({','.join(data)}) "
        f"VALUES({','.join('?' for _ in data)})",
        tuple(data.values()),
    )
    return record.id


def _knowledge(
    conn: sqlite3.Connection,
    request: SearchRequest | OpenRequest | None,
    context: ReviewContext,
) -> tuple[object, tuple[str, ...]]:
    """Resolve content and supports only through authoritative public APIs."""
    if request is None:
        value = knowledge_push.knowledge_context(
            conn,
            context.scope,
            min(context.policy.context_bytes, context.policy.max_input_bytes),
            time.time(),
        )
        revisions = [
            (item.id, item.revision) for item in (*value.notes, *value.guides)
        ]
        content: object = value.model_dump(mode="json")
    else:
        views = (
            knowledge_read.search_knowledge(conn, request.query, request.scope)
            if isinstance(request, SearchRequest)
            else (
                knowledge_read.show_knowledge(
                    conn, request.knowledge_id, request.revision
                ),
            )
        )
        revisions = [(view.id, view.revision) for view in views]
        content = (
            [view.model_dump(mode="json") for view in views]
            if isinstance(request, SearchRequest)
            else views[0].model_dump(mode="json")
        )
    ids: set[str] = set()
    for ident, revision in revisions:
        ids.update(
            knowledge_read.knowledge_support(
                conn, ident, revision, context.scope, time.time()
            )
        )
    return content, tuple(sorted(ids))


def _response(value: object, context: ReviewContext) -> bytes:
    """Refuse oversized canonical responses without partial admission."""
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if len(raw) > min(
        context.policy.context_bytes, context.policy.max_input_bytes
    ):
        raise ValueError("context_limit")
    return raw


def execute_read(
    home: Path, request: RoundRequest, context: ReviewContext
) -> bytes:
    """Commit evidence admission before returning bounded read content."""
    context = ReviewContext.model_validate_json(canonical_json(context))
    request = parse_round(canonical_json(request).encode(), context)
    if isinstance(request, FinalRequest):
        raise ValueError("final_not_read")
    return _execute(home, request, context)


def prepare_context(home: Path, context: ReviewContext) -> bytes:
    """Admit note and guide-title supports before returning context."""
    context = ReviewContext.model_validate_json(canonical_json(context))
    check_round(context)
    return _execute(home, None, context)


def _execute(
    home: Path,
    request: SearchRequest | OpenRequest | EvidenceRequest | None,
    context: ReviewContext,
) -> bytes:
    """Own one writer transaction and aggregate original-read allowance."""
    with (
        store.writer_lock(
            home, wait_s=max(0.0, context.deadline - time.monotonic())
        ),
        closing(store.connect_rw(store.db_path(home))) as conn,
    ):
        conn.execute("BEGIN IMMEDIATE")
        try:
            check_round(context)
            budget = ReadBudget(context.policy.max_input_bytes)
            source, original = _current(
                conn,
                context,
                budget,
                (
                    request.locator.line
                    if isinstance(request, EvidenceRequest)
                    else None
                ),
            )
            if isinstance(request, EvidenceRequest):
                ident = _selection(conn, request, source, original)
                records = validate_evidence(
                    conn,
                    home,
                    (ident,),
                    context.scope,
                    context.policy,
                    budget=budget,
                )
                content = records[0].model_dump(mode="json")
                ids = (ident,)
            else:
                content, ids = _knowledge(conn, request, context)
                validate_evidence(
                    conn,
                    home,
                    ids,
                    context.scope,
                    context.policy,
                    budget=budget,
                )
            raw = _response(content, context)
            _link(conn, context, ids)
            check_round(context)
            conn.commit()
            return raw
        except BaseException:
            conn.rollback()
            raise
