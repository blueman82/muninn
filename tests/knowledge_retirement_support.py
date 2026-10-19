"""Build normalized native-proof content through checked local proposals."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest import mock
from uuid import uuid4

from muninn import cli_learn, review_evidence, review_requests, store
from muninn.knowledge_models import ReviewResult
from muninn.review_evidence_models import (
    EvidenceRecord,
    EvidenceWindow,
    SourceRef,
)
from muninn.review_json import canonical_json
from muninn.review_models import ProposalInput, ReviewPolicy
from muninn.review_progress import digest
from tests.test_review_shared_models import policy_data


def _persist(conn: sqlite3.Connection, record: EvidenceRecord) -> str:
    """Persist real selected evidence without changing its authority fields."""
    row = conn.execute(
        "SELECT id FROM review_evidence WHERE provider=? AND thread_id=? "
        "AND line=? AND part=? AND line_sha256=? AND projection_version=? "
        "AND selector_digest=?",
        (
            record.provider,
            record.thread_id,
            record.line,
            record.part,
            record.line_sha256,
            record.projection_version,
            record.selector_digest,
        ),
    ).fetchone()
    if row is not None:
        return row[0]
    data = record.model_dump(mode="json")
    for key in ("scope", "selector", "original_locator"):
        data[f"{key}_json"] = json.dumps(data.pop(key))
    conn.execute(
        f"INSERT INTO review_evidence({','.join(data)}) "
        f"VALUES({','.join('?' for _ in data)})",
        tuple(data.values()),
    )
    return record.id


def native_note(home: Path, roots: dict[str, Path], text: str) -> None:
    """Commit one supported synthetic note without any reviewer invocation."""
    with closing(store.connect_ro(store.db_path(home))) as conn:
        row = conn.execute(
            "SELECT s.provider,s.thread_id,e.line,e.line_sha256 FROM source s "
            "JOIN event e ON e.source_id=s.id WHERE e.kind='prompt'"
        ).fetchone()
    window = EvidenceWindow(
        source=SourceRef(provider=row[0], thread_id=row[1]),
        from_line=row[2],
        through_line=row[2],
        boundary_hash=row[3],
    )
    window_note(home, roots, window, text)


def window_note(
    home: Path,
    roots: dict[str, Path],
    window: EvidenceWindow,
    text: str,
    *,
    max_bytes: int = 4096,
    allow_global: bool = False,
    extra_windows: tuple[EvidenceWindow, ...] = (),
    **updates: object,
) -> str:
    """Commit a normalized note backed by selected physical fixture bytes."""
    env = {"MUNINN_ROOTS": json.dumps({k: str(v) for k, v in roots.items()})}
    with mock.patch.dict(os.environ, env):
        selected = [
            record
            for admitted in (window, *extra_windows)
            for record in review_evidence.initial_evidence(
                home, admitted, max_bytes
            ).excerpts
        ]
        assert selected
        evidence = selected[0]
        assert all(record.scope == evidence.scope for record in selected)
        data = policy_data()
        data.update(
            projection_version="permitted-fields-v2",
            max_input_bytes=max_bytes,
            max_output_bytes=max_bytes,
            allow_global=allow_global or evidence.scope.project_id is None,
            policy_id="fixture-" + uuid4().hex,
        )
        policy = ReviewPolicy.model_validate(data)
        with (
            store.writer_lock(home),
            closing(store.connect_rw(store.db_path(home))) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE review_control SET admission=1,generation=1,"
                "policy_json=?,policy_digest=?,activated_at=10.0",
                (canonical_json(policy), digest(policy)),
            )
            request = review_requests.enqueue_review(
                conn, window, evidence.scope, policy, "explicit", 11.0
            )
            ids = list(
                dict.fromkeys(_persist(conn, item) for item in selected)
            )
            conn.executemany(
                "INSERT INTO review_input VALUES(?,?)",
                [(request.id, ident) for ident in ids],
            )
            conn.commit()
        result = ReviewResult.model_validate(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "action": "add",
                        "form": "note",
                        "kind": "decision",
                        "scope": evidence.scope.model_dump(),
                        "content": text,
                        "conditions": [],
                        "evidence_refs": ids,
                        "reason": "Synthetic direct-owner instruction.",
                        **updates,
                    }
                ],
                "unresolved": [],
                "no_change_reason": None,
            }
        )
        receipt = cli_learn.propose(
            home, ProposalInput(request_id=request.id, result=result)
        )
        assert receipt.outcome == "completed" and len(receipt.applied) == 1
        return receipt.applied[0].knowledge_id
