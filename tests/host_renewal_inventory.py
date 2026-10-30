"""Populate synthetic governed inventory without coupling test assertions."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from muninn import ingest, store
from muninn.knowledge_models import Scope
from muninn.review_evidence_models import EvidenceWindow, SourceRef
from muninn.review_progress import digest
from tests import host_renewal_support
from tests.ingest_support import TID, Record, line, primary, rollout


def seed_inventory(self: host_renewal_support.HostFixture) -> None:
    """Populate every inventoried table without provider source bodies."""
    request = self.request("completed")
    attempt = self.attempt(request)
    self.sql(
        "UPDATE review_attempt SET ended_at=3.0,progress_at=3.0,"
        "outcome='completed'"
    )
    self.sql(
        "UPDATE review_request SET state='completed',"
        "current_attempt_id=NULL"
    )
    self.insert(
        "review_source",
        {
            "provider": "codex",
            "thread_id": "fixture",
            "activation_line": 0,
            "examined_line": 1,
            "eligible_line": 1,
            "completed_line": 1,
            "updated_at": 1.0,
        },
    )
    self.insert(
        "source",
        {
            "provider": "codex",
            "thread_id": "fixture",
            "session_root": "fixture",
            "thread_class": "primary",
            "class_reason": "synthetic",
            "replay_mode": "none",
            "root": "codex-sessions",
            "path": "synthetic.jsonl",
            "first_line_sha256": "a" * 64,
            "ino": 1,
            "size": 0,
            "mtime_ns": 1,
            "classifier_version": 1,
            "first_seen": 1.0,
            "last_seen": 1.0,
        },
    )
    self.insert(
        "tombstone",
        {
            "provider": "codex",
            "level": "thread",
            "thread_id": "erased-fixture",
            "created_at": 1.0,
        },
    )
    self.insert(
        "learning_setup",
        {"singleton": 1, "mode": "now", "selected_at": 1.0},
    )
    self.insert(
        "review_issue",
        {
            "id": "fixture",
            "code": "config",
            "stage": "startup",
            "cause": "safe",
            "action": "safe",
            "created_at": 1.0,
        },
    )
    self.insert(
        "review_call",
        {
            "attempt_id": attempt,
            "round_index": 1,
            "budget_account_id": "synthetic-account",
            "policy_digest": digest(self.old),
            "state": "completed",
            "reserved_at": 2.0,
            "requested_json": "{}",
            "observed_json": "{}",
        },
    )
    self.insert(
        "review_receipt",
        {
            "request_id": request,
            "attempt_id": attempt,
            "applied_json": "[]",
            "unresolved_json": "[]",
            "committed_at": 3.0,
            "outcome": "no_change",
        },
    )
    seed_knowledge(self, request)


def seed_knowledge(
    self: host_renewal_support.HostFixture, request: str
) -> None:
    """Add linked synthetic evidence, knowledge and decisions."""
    evidence, knowledge, candidate = (
        "E_" + "1" * 32,
        "K_" + "1" * 32,
        "candidate",
    )
    self.insert(
        "review_evidence",
        {
            "id": evidence,
            "provider": "codex",
            "thread_id": "fixture",
            "session_root": "fixture",
            "line": 1,
            "part": 1,
            "line_sha256": "a" * 64,
            "source_version": "synthetic",
            "projection_version": "permitted-fields-v2",
            "original_locator_json": "{}",
            "selector_json": "{}",
            "selector_digest": "b" * 64,
            "evidence_type": "message",
            "authority": "owner",
            "scope_json": "{}",
            "state": "live",
            "selected_at": 1.0,
        },
    )
    self.insert(
        "review_input", {"request_id": request, "evidence_id": evidence}
    )
    self.insert(
        "knowledge",
        {"id": knowledge, "current_revision": 1, "state": "active"},
    )
    self.insert(
        "knowledge_version",
        {
            "knowledge_id": knowledge,
            "revision": 1,
            "request_id": request,
            "form": "note",
            "kind": "fact",
            "scope_json": "{}",
            "content_json": "{}",
            "origin": "review",
            "created_at": 3.0,
        },
    )
    self.insert(
        "knowledge_claim",
        {
            "knowledge_id": knowledge,
            "revision": 1,
            "claim_key": "claim",
            "text": "synthetic",
            "dependency_keys_json": "[]",
            "state": "live",
        },
    )
    self.insert(
        "knowledge_support",
        {
            "knowledge_id": knowledge,
            "revision": 1,
            "claim_key": "claim",
            "evidence_id": evidence,
        },
    )
    self.insert(
        "unresolved",
        {
            "id": "U_" + "1" * 32,
            "request_id": request,
            "item_json": "{}",
            "affected_json": "[]",
            "support_json": "[]",
            "created_at": 3.0,
            "state": "open",
        },
    )
    seed_decisions(self, request, evidence, candidate)


def seed_decisions(
    self: host_renewal_support.HostFixture,
    request: str,
    evidence: str,
    candidate: str,
) -> None:
    """Add immutable synthetic candidates and their linked decisions."""
    self.insert(
        "memory_candidate",
        {
            "id": candidate,
            "request_id": request,
            "payload_json": "{}",
            "digest": "c" * 64,
            "normalization_version": 1,
            "enrichment_version": 1,
            "state": "waiting_verification",
            "created_at": 3.0,
        },
    )
    self.insert(
        "memory_decision",
        {
            "id": "decision",
            "candidate_id": candidate,
            "candidate_digest": "c" * 64,
            "intent_key": "intent",
            "action": "reject",
            "submitted_at": 4.0,
            "state": "waiting_verification",
        },
    )
    self.insert(
        "memory_candidate_support",
        {
            "candidate_id": candidate,
            "evidence_id": evidence,
            "role": "original",
        },
    )


@dataclass
class IndexedSource:
    """Retain only one disposable provider tree and its original records."""

    roots: dict[str, Path]
    path: Path
    records: list[Record]
    scope: Scope

    def window(self, first: int = 2, last: int = 3) -> EvidenceWindow:
        """Build an exact interval from retained originals."""
        return EvidenceWindow(
            source=SourceRef(provider="codex", thread_id=TID),
            from_line=first,
            through_line=last,
            boundary_hash=hashlib.sha256(
                line(self.records[last - 1]).rstrip(b"\r\n")
            ).hexdigest(),
        )


def seed_indexed(fixture: host_renewal_support.HostFixture) -> IndexedSource:
    """Index a synthetic primary source only under this test's HOME."""
    roots = {
        name: fixture.home / "synthetic" / name
        for name in ("codex-sessions", "codex-archived", "claude-projects")
    }
    for root in roots.values():
        root.mkdir(parents=True)
    path = roots["codex-sessions"] / rollout()
    path.parent.mkdir(parents=True)
    records = primary(cwd=str(fixture.home / "project"))
    path.write_bytes(b"".join(map(line, records)))
    fixture.enterContext(
        mock.patch.dict(
            os.environ,
            {
                "MUNINN_HOME": str(fixture.ctx.data),
                "MUNINN_ROOTS": json.dumps(
                    {name: str(path) for name, path in roots.items()}
                ),
            },
        )
    )
    with (
        store.writer_lock(fixture.ctx.data),
        closing(store.connect_rw(fixture.path)) as conn,
    ):
        ingest.ingest(conn, roots)
        scope = conn.execute(
            "SELECT s.key FROM scope s JOIN event e ON s.id=e.scope_id LIMIT 1"
        ).fetchone()[0]
    return IndexedSource(
        roots, path, records, Scope(project_id=scope, task=None)
    )
