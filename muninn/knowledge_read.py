"""Read retained history and authoritative eligible current knowledge."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager

from muninn.knowledge_models import (
    AffectedRevision,
    EvidenceId,
    Expiry,
    KnowledgeId,
    KnowledgeView,
    Scope,
    UnresolvedId,
)
from muninn.knowledge_scope import scope_applies
from muninn.knowledge_write import claims, json_value
from muninn.query.terms import build_fts_query
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceRecord,
    SourceRef,
)
from muninn.review_models import ReviewPolicy
from muninn.review_selectors import (
    PROJECTION_VERSION,
    SOURCE_VERSIONS,
    check_tombstones,
)

_SELECT = (
    "SELECT k.*,v.revision,v.form,v.kind,v.scope_json,v.content_json,"
    "v.origin,v.expiry_json,v.created_at,v.erased_at,v.request_id "
    "FROM knowledge k JOIN knowledge_version v ON v.knowledge_id=k.id "
)


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Generator[None]:
    """Keep multi-query eligibility coherent without owning a writer lock."""
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            conn.rollback()


def _view(row: sqlite3.Row) -> KnowledgeView:
    """Render version content while keeping erasure dominant over history."""
    erased = row["state"] == "erased" or row["erased_at"] is not None
    return KnowledgeView.model_validate_json(
        json_value(
            {
                "schema_version": 1,
                "id": row["id"],
                "revision": row["revision"],
                "state": "erased" if erased else row["state"],
                "body": (
                    json.loads(row["content_json"])
                    if not erased and row["content_json"] is not None
                    else None
                ),
                "origin": row["origin"],
                "expiry": (
                    json.loads(row["expiry_json"])
                    if not erased and row["expiry_json"] is not None
                    else None
                ),
                "created_at": row["created_at"],
                "retracted_at": row["retracted_at"],
                "retracted_reason": (
                    None if erased else row["retracted_reason"]
                ),
            }
        )
    )


def show_knowledge(
    conn: sqlite3.Connection,
    knowledge_id: KnowledgeId,
    revision: int | None,
) -> KnowledgeView:
    """Display retained current state, or an explicitly selected revision.

    Args:
        conn: Open store connection; any existing transaction is preserved.
        knowledge_id: Explicit knowledge identity, never an integer alias.
        revision: Historical revision, or None to select the current one.

    Returns:
        Retained content and lifecycle state; this grants no read admission.

    Raises:
        ValueError: If the identity/revision is invalid or unavailable.
    """
    if revision is not None:
        AffectedRevision(knowledge_id=knowledge_id, revision=revision)
    with read_snapshot(conn):
        row = conn.execute(
            _SELECT + "WHERE k.id=? AND "
            "v.revision=coalesce(?,k.current_revision)",
            (knowledge_id, revision),
        ).fetchone()
        if row is None:
            raise ValueError("knowledge_not_found")
        return _view(row)


def _scope_allowed(conn: sqlite3.Connection, scope: Scope) -> bool:
    """Require a mapped project or explicit active global policy permission."""
    if scope.project_id is not None:
        return (
            conn.execute(
                "SELECT 1 FROM scope WHERE key=? AND kind!='global'",
                (scope.project_id,),
            ).fetchone()
            is not None
        )
    row = conn.execute(
        "SELECT admission,policy_json FROM review_control WHERE singleton=1"
    ).fetchone()
    if row is None or not row["admission"] or row["policy_json"] is None:
        return False
    return ReviewPolicy.model_validate_json(row["policy_json"]).allow_global


def _disputed(conn: sqlite3.Connection, ident: str, revision: int) -> bool:
    """Check exact affected revision associations even if state/FTS lag."""
    return (
        conn.execute(
            "SELECT 1 FROM unresolved u,json_each(u.affected_json) a "
            "WHERE u.state='open' "
            "AND json_extract(a.value,'$.knowledge_id')=? "
            "AND json_extract(a.value,'$.revision')=? LIMIT 1",
            (ident, revision),
        ).fetchone()
        is not None
    )


def _evidence(
    conn: sqlite3.Connection, ident: EvidenceId, scope: Scope
) -> sqlite3.Row:
    """Require current governed metadata and non-erased physical provenance."""
    row = conn.execute(
        "SELECT * FROM review_evidence WHERE id=?", (ident,)
    ).fetchone()
    if row is None or row["state"] != "live" or row["excerpt"] is None:
        raise ValueError("knowledge_ineligible")
    data = dict(row)
    for key in ("scope", "selector", "original_locator"):
        data[key] = json.loads(data.pop(f"{key}_json"))
    record = EvidenceRecord.model_validate_json(json_value(data))
    if (
        not scope_applies(record.scope, scope)
        or row["projection_version"] != PROJECTION_VERSION
        or row["source_version"] != SOURCE_VERSIONS.get(row["provider"])
    ):
        raise ValueError("knowledge_ineligible")
    source = conn.execute(
        "SELECT * FROM source WHERE provider=? AND thread_id=? "
        "AND status='active' AND thread_class='primary' AND session_root=? "
        "AND replay_mode!='unverified'",
        (row["provider"], row["thread_id"], row["session_root"]),
    ).fetchone()
    if source is None:
        raise ValueError("knowledge_ineligible")
    check_tombstones(
        conn,
        source,
        EvidenceLocator(
            source=SourceRef(
                provider=row["provider"], thread_id=row["thread_id"]
            ),
            line=row["line"],
            part=row["part"],
            line_sha256=row["line_sha256"],
        ),
    )
    indexed = conn.execute(
        "SELECT line_sha256 FROM event WHERE source_id=? AND line=? "
        "AND part=?",
        (source["id"], row["line"], row["part"]),
    ).fetchone()
    if indexed is not None and indexed[0] != row["line_sha256"]:
        raise ValueError("knowledge_ineligible")
    return row


def _supports(
    conn: sqlite3.Connection, view: KnowledgeView
) -> tuple[EvidenceId, ...]:
    """Resolve associations only after exact normalized claim agreement."""
    body = view.body
    if body is None:
        raise ValueError("knowledge_ineligible")
    expected = claims(body)
    stored = {
        row["claim_key"]: row
        for row in conn.execute(
            "SELECT * FROM knowledge_claim WHERE knowledge_id=? "
            "AND revision=?",
            (view.id, view.revision),
        )
    }
    if set(stored) != {claim[0] for claim in expected}:
        raise ValueError("knowledge_ineligible")
    supports: set[EvidenceId] = set()
    owner = body.kind in {"decision", "preference"} or (
        body.scope.project_id is None
    )
    for key, text, refs, dependencies in expected:
        row = stored[key]
        ids = {
            row[0]
            for row in conn.execute(
                "SELECT evidence_id FROM knowledge_support "
                "WHERE knowledge_id=? AND revision=? AND claim_key=?",
                (view.id, view.revision, key),
            )
        }
        if (
            row["state"] != "live"
            or row["text"] != text
            or json.loads(row["dependency_keys_json"]) != dependencies
            or ids != set(refs)
        ):
            raise ValueError("knowledge_ineligible")
        records = [_evidence(conn, ident, body.scope) for ident in ids]
        if owner and not any(
            row["authority"] == "direct_owner"
            and row["evidence_type"] == "owner_statement"
            for row in records
        ):
            raise ValueError("knowledge_ineligible")
        supports.update(ids)
    return tuple(sorted(supports))


def _expiry_supports(
    conn: sqlite3.Connection, row: sqlite3.Row, expiry: Expiry, scope: Scope
) -> tuple[EvidenceId, ...]:
    """Bind explicit expiry associations to the successful version input."""
    receipt = conn.execute(
        "SELECT 1 FROM review_receipt r,json_each(r.applied_json) a "
        "WHERE r.request_id=? "
        "AND json_extract(a.value,'$.knowledge_id')=? "
        "AND json_extract(a.value,'$.revision')=? LIMIT 1",
        (row["request_id"], row["id"], row["revision"]),
    ).fetchone()
    if receipt is None:
        raise ValueError("knowledge_ineligible")
    for ident in expiry.evidence_refs:
        admitted = conn.execute(
            "SELECT 1 FROM review_input WHERE request_id=? AND evidence_id=?",
            (row["request_id"], ident),
        ).fetchone()
        if admitted is None:
            raise ValueError("knowledge_ineligible")
        _evidence(conn, ident, scope)
    return tuple(expiry.evidence_refs)


def eligible_current(
    conn: sqlite3.Connection, row: sqlite3.Row, scope: Scope, now: float
) -> tuple[KnowledgeView, tuple[EvidenceId, ...]]:
    """Apply one shared current-state, scope, expiry and support gate."""
    if row["state"] != "active" or row["revision"] != row["current_revision"]:
        raise ValueError("knowledge_ineligible")
    view = _view(row)
    body = view.body
    stored = Scope.model_validate_json(row["scope_json"])
    if (
        body is None
        or body.scope != stored
        or body.form != row["form"]
        or body.kind != row["kind"]
        or not scope_applies(stored, scope)
        or _disputed(conn, view.id, view.revision)
        or (
            view.expiry is not None
            and view.expiry.at is not None
            and view.expiry.at <= now
        )
    ):
        raise ValueError("knowledge_ineligible")
    supports = set(_supports(conn, view))
    if view.expiry is not None:
        supports.update(_expiry_supports(conn, row, view.expiry, stored))
    return view, tuple(sorted(supports))


def knowledge_support(
    conn: sqlite3.Connection,
    knowledge_id: KnowledgeId,
    revision: int,
    scope: Scope,
    now: float,
) -> tuple[EvidenceId, ...]:
    """Resolve admitted support for an exact permitted current revision.

    Args:
        conn: Open store connection; existing transactions are preserved.
        knowledge_id: Program-selected knowledge identity.
        revision: Exact current revision to authorize.
        scope: Explicit mapped scope; global needs active policy permission.
        now: Explicit finite nonnegative time for expiry evaluation.

    Returns:
        Deterministic unique IDs from checked stored claim/support rows.
        Physical original revalidation and request admission remain the
        controlled caller's responsibility before it returns content.

    Raises:
        ValueError: If input is invalid or knowledge/support is ineligible.
    """
    AffectedRevision(knowledge_id=knowledge_id, revision=revision)
    validate_time(now)
    with read_snapshot(conn):
        row = conn.execute(
            _SELECT + "AND v.revision=k.current_revision WHERE k.id=?",
            (knowledge_id,),
        ).fetchone()
        if row is None or not _scope_allowed(conn, scope):
            raise ValueError("knowledge_ineligible")
        if row["revision"] != revision:
            raise ValueError("knowledge_ineligible")
        try:
            return eligible_current(conn, row, scope, now)[1]
        except ValueError:
            raise ValueError("knowledge_ineligible") from None


def validate_time(now: float) -> None:
    """Reject invalid evaluation clocks without guessing a replacement."""
    if isinstance(now, bool) or not math.isfinite(now) or now < 0:
        raise ValueError("knowledge_time")


def candidate_rows(
    conn: sqlite3.Connection,
    scope: Scope,
    fts: str | None = None,
    limit: int | None = None,
) -> Iterator[sqlite3.Row]:
    """Stream current active scope-filtered rows before full validation."""
    if not _scope_allowed(conn, scope):
        return
    sql = _SELECT + "AND v.revision=k.current_revision "
    where = [
        "k.state='active'",
        "json_extract(v.scope_json,'$.project_id') IS ?",
        "(json_extract(v.scope_json,'$.task') IS NULL OR "
        "json_extract(v.scope_json,'$.task')=?)",
    ]
    params: list[object] = [scope.project_id, scope.task]
    if fts is not None:
        sql += (
            "JOIN knowledge_fts f ON f.knowledge_id=k.id "
            "AND f.revision=v.revision "
        )
        where.append("knowledge_fts MATCH ?")
        params.append(fts)
    sql += f"WHERE {' AND '.join(where)} ORDER BY v.created_at DESC,k.id DESC"
    if limit is not None:
        sql = sql.replace(
            "SELECT k.*", "SELECT count(*) OVER() AS candidate_total,k.*", 1
        )
        sql += " LIMIT ?"
        params.append(limit)
    yield from conn.execute(sql, params)


def current_knowledge(
    conn: sqlite3.Connection, scope: Scope, now: float, fts: str | None = None
) -> Iterator[KnowledgeView]:
    """Stream eligible current candidates inside the caller's snapshot."""
    validate_time(now)
    seen: set[KnowledgeId] = set()
    for row in candidate_rows(conn, scope, fts):
        try:
            view, _ = eligible_current(conn, row, scope, now)
        except ValueError:
            continue
        if view.id not in seen:
            seen.add(view.id)
            yield view


def search_knowledge(
    conn: sqlite3.Connection, query: str, scope: Scope
) -> tuple[KnowledgeView, ...]:
    """Search only authoritative current eligible scoped FTS candidates."""
    fts = build_fts_query(query)
    if fts is None:
        return ()
    with read_snapshot(conn):
        return tuple(current_knowledge(conn, scope, time.time(), fts))


def unresolved_ids(
    conn: sqlite3.Connection, scope: Scope
) -> list[UnresolvedId]:
    """List open questions affecting retained current applicable revisions."""
    if not _scope_allowed(conn, scope):
        return []
    rows = conn.execute(
        "SELECT DISTINCT u.id,v.scope_json FROM unresolved u,"
        "json_each(u.affected_json) a JOIN knowledge k "
        "ON k.id=json_extract(a.value,'$.knowledge_id') "
        "JOIN knowledge_version v ON v.knowledge_id=k.id "
        "AND v.revision=k.current_revision "
        "WHERE u.state='open' AND k.state!='erased' "
        "AND v.revision=json_extract(a.value,'$.revision') ORDER BY u.id"
    )
    return sorted(
        {
            row["id"]
            for row in rows
            if scope_applies(
                Scope.model_validate_json(row["scope_json"]), scope
            )
        }
    )
