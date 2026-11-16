"""Persist immutable, content-free acceptance of historical uncertainty."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from muninn.knowledge_models import (
    AttemptId,
    Digest,
    RequestId,
    Revision,
    StrictModel,
    UtcSeconds,
)
from muninn.review_state import available

ACK_SQL = """
CREATE TABLE IF NOT EXISTS review_unknown_ack (
 acknowledgement_id TEXT NOT NULL UNIQUE,
 attempt_id TEXT NOT NULL, round_index INTEGER NOT NULL CHECK(round_index>0),
 request_id TEXT NOT NULL REFERENCES review_request(id) ON DELETE CASCADE,
 call_digest TEXT NOT NULL, attempt_digest TEXT NOT NULL,
 request_digest TEXT NOT NULL, request_policy_digest TEXT NOT NULL,
 call_policy_digest TEXT NOT NULL, policy_digest TEXT NOT NULL,
 old_generation INTEGER NOT NULL CHECK(old_generation>=0),
 resulting_generation INTEGER NOT NULL
   CHECK(resulting_generation=old_generation+1),
 acknowledged_at REAL NOT NULL CHECK(acknowledged_at>=0),
 accept_uncertainty INTEGER NOT NULL CHECK(accept_uncertainty=1),
 PRIMARY KEY(attempt_id,round_index),
 FOREIGN KEY(attempt_id,round_index)
   REFERENCES review_call(attempt_id,round_index) ON DELETE CASCADE);
CREATE TRIGGER IF NOT EXISTS review_unknown_ack_immutable
 BEFORE UPDATE ON review_unknown_ack BEGIN
 SELECT RAISE(ABORT, 'acknowledgement is immutable'); END;
"""


class UnknownAcknowledgement(StrictModel):
    """Bind owner acceptance to historical rows and a generation fence."""

    acknowledgement_id: Annotated[str, Field(pattern=r"^UA_[0-9a-f]{32}$")]
    attempt_id: AttemptId
    round_index: Revision
    request_id: RequestId
    call_digest: Digest
    attempt_digest: Digest
    request_digest: Digest
    request_policy_digest: Digest
    call_policy_digest: Digest
    policy_digest: Digest
    old_generation: Annotated[int, Field(ge=0)]
    resulting_generation: Revision
    acknowledged_at: UtcSeconds
    accept_uncertainty: Literal[True]

    @model_validator(mode="after")
    def adjacent_generations(self) -> Self:
        """Require the acknowledged transition to advance exactly once."""
        if self.resulting_generation != self.old_generation + 1:
            raise ValueError("recovery_refused")
        return self


def ensure(conn: sqlite3.Connection) -> None:
    """Create auxiliary metadata only inside a caller-owned transaction."""
    if not conn.in_transaction:
        raise ValueError("caller_transaction_required")
    statement = ""
    for line in ACK_SQL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""


def read(
    conn: sqlite3.Connection, attempt: AttemptId, round_index: int
) -> UnknownAcknowledgement | None:
    """Read existing metadata without creating a table in older stores."""
    if not available(conn, "review_unknown_ack"):
        return None
    row = conn.execute(
        "SELECT * FROM review_unknown_ack WHERE attempt_id=? "
        "AND round_index=?",
        (attempt, round_index),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    if data["accept_uncertainty"] != 1:
        raise ValueError("recovery_refused")
    data["accept_uncertainty"] = True
    return UnknownAcknowledgement.model_validate(data)


def save(
    conn: sqlite3.Connection, acknowledgement: UnknownAcknowledgement
) -> None:
    """Insert the checked record without replacing an earlier decision."""
    ensure(conn)
    values = acknowledgement.model_dump()
    conn.execute(
        "INSERT INTO review_unknown_ack VALUES("
        + ",".join("?" for _ in values)
        + ")",
        tuple(values.values()),
    )
