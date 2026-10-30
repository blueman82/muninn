"""Traverse and scrub governed decision content under caller ownership."""

from __future__ import annotations

import sqlite3

from muninn.knowledge_erase_state import SupportClosure
from muninn.tombstone_intent import IntentRow


def collect_links(conn: sqlite3.Connection, plan: SupportClosure) -> bool:
    """Grow decision, source, application and historical-proof associations."""
    before = plan.sizes()
    for evidence in tuple(plan.evidence):
        plan.candidates.update(
            r[0]
            for r in conn.execute(
                "SELECT candidate_id FROM memory_candidate_support WHERE "
                "evidence_id=?",
                (evidence,),
            )
        )
        plan.decisions.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM memory_decision WHERE owner_evidence_id=? "
                "OR display_evidence_id=?",
                (evidence, evidence),
            )
        )
    for request in tuple(plan.requests):
        plan.candidates.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM memory_candidate WHERE request_id=? "
                "OR application_request_id=? UNION SELECT candidate_id FROM "
                "review_request WHERE id=? AND candidate_id IS NOT NULL",
                (request, request, request),
            )
        )
    for unresolved in tuple(plan.unresolved):
        plan.candidates.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM memory_candidate WHERE origin_unresolved_id=?",
                (unresolved,),
            )
        )
    for ident, revision in tuple(plan.versions):
        plan.candidates.update(
            r[0]
            for r in conn.execute(
                "SELECT c.id FROM memory_candidate c,json_each(c.payload_json,"
                "'$.targets') t WHERE "
                "json_extract(t.value,'$.knowledge_id')=? "
                "AND json_extract(t.value,'$.revision')=?",
                (ident, revision),
            )
        )
    _candidate_links(conn, plan)
    _decision_links(conn, plan)
    return before != plan.sizes()


def _candidate_links(conn: sqlite3.Connection, plan: SupportClosure) -> None:
    """Reach every history row and both ordinary and application requests."""
    for ident in tuple(plan.candidates):
        plan.decisions.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM memory_decision WHERE candidate_id=? OR "
                "replacement_candidate_id=?",
                (ident, ident),
            )
        )
        for row in conn.execute(
            "SELECT request_id,application_request_id,origin_unresolved_id "
            "FROM memory_candidate WHERE id=?",
            (ident,),
        ):
            plan.requests.update(value for value in row[:2] if value)
            if row[2]:
                plan.unresolved.add(row[2])
        plan.requests.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM review_request WHERE candidate_id=?",
                (ident,),
            )
        )
        plan.evidence.update(
            r[0]
            for r in conn.execute(
                "SELECT evidence_id FROM memory_candidate_support WHERE "
                "candidate_id=? AND role IN ('owner','display')",
                (ident,),
            )
        )


def _decision_links(conn: sqlite3.Connection, plan: SupportClosure) -> None:
    """Follow accepted edit dependencies without erasing unrelated losers."""
    for ident in tuple(plan.decisions):
        for row in conn.execute(
            "SELECT candidate_id,replacement_candidate_id,accepted_revision,"
            "owner_evidence_id,display_evidence_id FROM memory_decision "
            "WHERE id=?",
            (ident,),
        ):
            plan.candidates.add(row[0])
            if row[1] and row[2] is not None:
                plan.candidates.add(row[1])
            plan.evidence.update(value for value in row[3:] if value)


def locator_rows(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, IntentRow], ...]:
    """Read exact pending and historical coordinates before body scrubbing."""
    rows: list[tuple[str, IntentRow]] = []
    for field in ("owner_locator", "display_locator"):
        rows.extend(
            (
                row[0],
                IntentRow(
                    row[1], "line", None, row[2], row[3], row[4], row[5]
                ),
            )
            for row in conn.execute(
                "SELECT d.id,json_extract(d.locator_json,?),"
                "json_extract(d.locator_json,?),json_extract(d.locator_json,?),"
                "json_extract(d.locator_json,?),d.submitted_at "
                "FROM memory_decision d WHERE d.locator_json IS NOT NULL",
                tuple(
                    f"$.{field}.{suffix}"
                    for suffix in (
                        "source.provider",
                        "source.thread_id",
                        "line",
                        "line_sha256",
                    )
                ),
            )
        )
    return tuple(rows)


def collect_intents(
    conn: sqlite3.Connection, plan: SupportClosure, rows: tuple[IntentRow, ...]
) -> None:
    """Match locator-only decisions before any proof evidence is admitted."""
    for ident, locator in locator_rows(conn):
        source = conn.execute(
            "SELECT session_root FROM source WHERE provider=? AND thread_id=?",
            (locator.provider, locator.thread_id),
        ).fetchone()
        roots = {locator.thread_id, source[0] if source else None}
        if any(
            row.provider == locator.provider
            and (
                (row.level == "session" and row.session_root in roots)
                or (
                    row.level == "thread"
                    and row.thread_id == locator.thread_id
                )
                or (
                    row.level == "line"
                    and row.thread_id == locator.thread_id
                    and (
                        row.line == locator.line
                        or row.line_sha256 == locator.line_sha256
                    )
                )
            )
            for row in rows
        ):
            plan.decisions.add(ident)


def collect_match(
    conn: sqlite3.Connection, plan: SupportClosure, text: str
) -> None:
    """Select decoded nested candidate and locator text without wildcards."""
    for table, column, targets in (
        ("memory_candidate", "payload_json", plan.candidates),
        ("memory_decision", "locator_json", plan.decisions),
    ):
        targets.update(
            r[0]
            for r in conn.execute(
                f"SELECT c.id FROM {table} c WHERE EXISTS(SELECT 1 FROM "
                f"json_tree(c.{column}) j WHERE j.type='text' "
                "AND instr(j.atom,?)>0)",
                (text,),
            )
        )


def scrub(conn: sqlite3.Connection, plan: SupportClosure, now: float) -> None:
    """Clear governed bodies and retain immutable accepted history."""
    if not conn.in_transaction:
        raise ValueError("caller_transaction_required")
    for ident in plan.decisions:
        conn.execute(
            "UPDATE memory_decision SET state='erased',locator_json=NULL,"
            "owner_evidence_id=NULL,display_evidence_id=NULL,"
            "code='decision_erased' "
            "WHERE id=? AND state!='erased'",
            (ident,),
        )
    for ident in plan.candidates:
        conn.execute(
            "UPDATE memory_candidate SET payload_json=NULL,state='erased',"
            "code='decision_erased' WHERE id=?",
            (ident,),
        )
        conn.execute(
            "DELETE FROM memory_candidate_support WHERE candidate_id=?",
            (ident,),
        )
