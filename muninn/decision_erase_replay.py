"""Expand replay to associated decision proof copies before scrubbing edges."""

from __future__ import annotations

import sqlite3

from muninn.erase_line_selection import decision_lines, evidence_lines
from muninn.erase_target import Target
from muninn.knowledge_erase import plan_replay_support, support_closure
from muninn.knowledge_erase_state import SupportClosure
from muninn.tombstone_intent import (
    IntentRow,
    TombstoneIntent,
    TombstoneRow,
    event_tag,
)
from muninn.tombstone_key import key_for
from muninn.tombstone_rows import tombstone_row


def expand_decision_intent(
    conn: sqlite3.Connection, intent: TombstoneIntent, plan: SupportClosure
) -> list[TombstoneRow]:
    """Plan proof coordinates and family content tags inside caller ownership.

    Persist returned rows before deleting indexed bodies or proof associations.
    Unselected live original inputs do not become reverse erasure targets.

    Args:
        conn: Current-two connection in the caller's replay transaction.
        intent: Complete previously verified table and journal intent.
        plan: Mutable governed closure seeded from that intent.

    Returns:
        Only new semantic intent keys, with no copied proof text.
    """
    if not plan.candidates:
        return []
    original = plan
    target = Target(review=plan)
    seen: set[str] = set()
    known = {row.key for row in intent.rows}
    while True:
        before = (plan.sizes(), len(target.lines), len(target.tombstones))
        _erased_application_inputs(conn, plan)
        plan = support_closure(
            conn,
            tuple(plan.evidence),
            request_ids=tuple(plan.requests),
            versions=tuple(plan.versions),
            unresolved_ids=tuple(plan.unresolved),
            candidate_ids=tuple(plan.candidates),
            decision_ids=tuple(plan.decisions),
        )
        decision_lines(conn, target, plan)
        evidence_lines(conn, target, plan.evidence - seen)
        seen.update(plan.evidence)
        extra = plan_replay_support(
            conn, tuple(IntentRow(**row) for row in target.tombstones)
        )
        plan.evidence.update(extra.evidence)
        plan.versions.update(extra.versions)
        plan.requests.update(extra.requests)
        plan.unresolved.update(extra.unresolved)
        plan.candidates.update(extra.candidates)
        plan.decisions.update(extra.decisions)
        if before == (plan.sizes(), len(target.lines), len(target.tombstones)):
            break
    if target.content:
        key = key_for(conn)
        target.tombstones.extend(
            tombstone_row(
                provider,
                "line",
                thread_id=thread,
                line=0,
                line_sha256=event_tag(key, role, text),
            )
            for provider, thread, role, text in target.content
        )
    output: list[TombstoneRow] = []
    for row in target.tombstones:
        ident = IntentRow(**row).key
        if ident not in known:
            known.add(ident)
            output.append(row)
    original.evidence.update(plan.evidence)
    original.versions.update(plan.versions)
    original.requests.update(plan.requests)
    original.unresolved.update(plan.unresolved)
    original.candidates.update(plan.candidates)
    original.decisions.update(plan.decisions)
    return output


def _erased_application_inputs(
    conn: sqlite3.Connection, plan: SupportClosure
) -> None:
    """Recover source coordinates already erased by a prior database scrub."""
    for candidate in tuple(plan.candidates):
        plan.evidence.update(
            row[0]
            for row in conn.execute(
                "SELECT i.evidence_id FROM review_input i JOIN "
                "review_request r "
                "ON r.id=i.request_id JOIN review_evidence e ON "
                "e.id=i.evidence_id "
                "WHERE r.candidate_id=? AND e.state='erased'",
                (candidate,),
            )
        )
