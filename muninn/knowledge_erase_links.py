"""Collect ordinary erasure identities without opening or changing a store."""

from __future__ import annotations

import sqlite3

from muninn.knowledge_erase_state import SupportClosure
from muninn.tombstone_intent import IntentRow


def _evidence_links(conn: sqlite3.Connection, plan: SupportClosure) -> None:
    """Reach admitted inputs, direct assertions and unresolved supports."""
    for ident in plan.evidence:
        plan.requests.update(
            r[0]
            for r in conn.execute(
                "SELECT request_id FROM review_input WHERE evidence_id=?",
                (ident,),
            )
        )
        plan.versions.update(
            (r[0], r[1])
            for r in conn.execute(
                "SELECT knowledge_id,revision FROM knowledge_support WHERE "
                "evidence_id=?",
                (ident,),
            )
        )
        plan.versions.update(
            (r[0], r[1])
            for r in conn.execute(
                "SELECT v.knowledge_id,v.revision FROM knowledge_version v,"
                "json_each(v.expiry_json,'$.evidence_refs') e WHERE e.value=?",
                (ident,),
            )
        )
        plan.unresolved.update(
            r[0]
            for r in conn.execute(
                "SELECT u.id FROM unresolved u,json_each(u.support_json) s "
                "WHERE s.value=?",
                (ident,),
            )
        )


def _version_links(conn: sqlite3.Connection, plan: SupportClosure) -> None:
    """Reach every explanation and conservative knowledge-read association."""
    for ident, revision in tuple(plan.versions):
        plan.requests.update(
            r[0]
            for r in conn.execute(
                "SELECT request_id FROM knowledge_version WHERE "
                "knowledge_id=? AND revision=?",
                (ident, revision),
            )
        )
        plan.requests.update(
            r[0]
            for r in conn.execute(
                "SELECT i.request_id FROM review_input i JOIN "
                "knowledge_support s "
                "ON s.evidence_id=i.evidence_id WHERE s.knowledge_id=? AND "
                "s.revision=?",
                (ident, revision),
            )
        )
        plan.requests.update(
            r[0]
            for r in conn.execute(
                "SELECT i.request_id FROM knowledge_version v,"
                "json_each(v.expiry_json,'$.evidence_refs') e JOIN "
                "review_input i "
                "ON i.evidence_id=e.value WHERE v.knowledge_id=? AND "
                "v.revision=?",
                (ident, revision),
            )
        )
        plan.unresolved.update(
            r[0]
            for r in conn.execute(
                "SELECT u.id FROM unresolved u,json_each(u.affected_json) a "
                "WHERE json_extract(a.value,'$.knowledge_id')=? "
                "AND json_extract(a.value,'$.revision')=?",
                (ident, revision),
            )
        )


def collect_links(conn: sqlite3.Connection, plan: SupportClosure) -> bool:
    """Grow only ordinary E/R/K/U associations in the caller's snapshot."""
    before = plan.sizes()
    _evidence_links(conn, plan)
    _version_links(conn, plan)
    _request_links(conn, plan)
    return before != plan.sizes()


def _request_links(conn: sqlite3.Connection, plan: SupportClosure) -> None:
    """Reach producing requests and every question's affected revisions."""
    for ident in tuple(plan.requests):
        plan.versions.update(
            (r[0], r[1])
            for r in conn.execute(
                "SELECT knowledge_id,revision FROM knowledge_version "
                "WHERE request_id=?",
                (ident,),
            )
        )
        plan.unresolved.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM unresolved WHERE request_id=?", (ident,)
            )
        )
    for ident in tuple(plan.unresolved):
        plan.requests.update(
            r[0]
            for r in conn.execute(
                "SELECT request_id FROM unresolved WHERE id=?", (ident,)
            )
        )
        plan.versions.update(
            (r[0], r[1])
            for r in conn.execute(
                "SELECT json_extract(a.value,'$.knowledge_id'),"
                "json_extract(a.value,'$.revision') FROM unresolved u,"
                "json_each(u.affected_json) a WHERE u.id=?",
                (ident,),
            )
        )


def intent_plan(
    conn: sqlite3.Connection, rows: tuple[IntentRow, ...]
) -> SupportClosure:
    """Seed ordinary identities from exact complete pre-insertion intent."""
    evidence: set[str] = set()
    requests: set[str] = set()
    for row in rows:
        values = (
            row.provider,
            row.level,
            row.session_root,
            row.thread_id,
            row.line,
            row.line_sha256,
        )
        intent = (
            "WITH t(provider,level,session_root,thread_id,line,line_sha256) "
            "AS (VALUES(?,?,?,?,?,?)) "
        )
        evidence.update(
            r[0]
            for r in conn.execute(
                intent + "SELECT e.id FROM review_evidence e JOIN t "
                "ON t.provider=e.provider WHERE "
                "(t.level='session' AND t.session_root=e.session_root) OR "
                "(t.level='thread' AND t.thread_id=e.thread_id) OR "
                "(t.level='line' AND t.thread_id=e.thread_id AND "
                "(t.line=e.line OR t.line_sha256=e.line_sha256))",
                values,
            )
        )
        requests.update(
            r[0]
            for r in conn.execute(
                intent + "SELECT r.id FROM review_request r JOIN t "
                "ON t.provider=r.provider WHERE "
                "(t.level='session' AND t.session_root=r.session_root) OR "
                "(t.level='thread' AND t.thread_id=r.thread_id) OR "
                "(t.level='line' AND t.thread_id=r.thread_id "
                "AND t.line BETWEEN r.from_line AND r.through_line)",
                values,
            )
        )
    return SupportClosure(evidence, set(), requests, set(), set(), set())
