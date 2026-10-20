"""Initialize first review policy and snapshot indexed source boundaries."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from contextlib import suppress

from muninn import store
from muninn.knowledge_models import StrictModel
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_selectors import PROJECTION_VERSION


def require_transaction(conn: sqlite3.Connection, now: float) -> None:
    """Require caller-owned atomic writes and a finite nonnegative clock."""
    if not conn.in_transaction:
        raise ValueError("caller_transaction_required")
    if type(now) is not float or not math.isfinite(now) or now < 0:
        raise ValueError("invalid_time")


def digest(value: StrictModel) -> str:
    """Bind an identity to the complete canonical model projection."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def checked_policy(policy: ReviewPolicy) -> ReviewPolicy:
    """Revalidate even copied models and require the accepted projection."""
    policy = ReviewPolicy.model_validate_json(canonical_json(policy))
    if policy.projection_version != PROJECTION_VERSION:
        raise ValueError("review_policy_required")
    return policy


def boundary(
    conn: sqlite3.Connection, source: sqlite3.Row
) -> tuple[int, str | None]:
    """Return the complete ingest cursor or exact native import boundary."""
    if source["provider"] == "cursor":
        if source["root"] != "cursor-imports" or not source["parse_state"]:
            raise ValueError("source_unavailable")
        row = conn.execute(
            "SELECT line,line_sha256 FROM event WHERE source_id=? "
            "ORDER BY line DESC LIMIT 1",
            (source["id"],),
        ).fetchone()
        return (row[0], row[1]) if row else (0, None)
    return source["cursor_line"], source["anchor_sha256"]


def activate_sources(conn: sqlite3.Connection, now: float) -> None:
    """Snapshot indexed history once when the caller enables review."""
    require_transaction(conn, now)
    for source in conn.execute("SELECT * FROM source"):
        last, last_hash = boundary(conn, source)
        conn.execute(
            "INSERT OR IGNORE INTO review_source VALUES(?,?,?,?,?,?,?,?)",
            (
                source["provider"],
                source["thread_id"],
                last,
                last,
                last,
                last,
                last_hash,
                now,
            ),
        )


def bootstrap_policy(
    conn: sqlite3.Connection, policy: ReviewPolicy, activated_at: float
) -> None:
    """Install the first policy and source baselines in the caller transaction.

    The caller holds the actual writer lock for this connection's store and
    owns its transaction and lifetime. This operation acquires no lock,
    opens no connection and commits nothing. It grants no native capability
    or policy approval. Configured state, including disabled admission,
    must use a separately authorized transition instead of this bootstrap.

    Args:
        conn: Caller-owned marked-store connection in an active transaction.
        policy: Explicit policy to revalidate and canonically snapshot.
        activated_at: Finite nonnegative floating-point activation time.

    Raises:
        ValueError: If the transaction, time, policy, source or initial state
            is invalid. Existing baselines and requests are never ignored.
        store.StoreUnavailableError: If the marked current format is absent.
        sqlite3.Error: If reading, writing or savepoint cleanup fails.
            The caller must roll back on failure; original errors prevail.
    """
    require_transaction(conn, activated_at)
    policy = checked_policy(policy)
    store.assert_store_format(conn)
    control = conn.execute(
        "SELECT singleton,admission,policy_json,policy_digest,activated_at "
        "FROM review_control"
    ).fetchall()
    if [tuple(row) for row in control] != [(1, 0, None, None, None)] or any(
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for table in ("review_source", "review_request")
    ):
        raise ValueError("review_bootstrap_state")
    conn.execute("SAVEPOINT review_policy_bootstrap")
    try:
        activate_sources(conn, activated_at)
        conn.execute(
            "UPDATE review_control SET admission=1,policy_json=?,"
            "policy_digest=?,activated_at=? WHERE singleton=1",
            (canonical_json(policy), digest(policy), activated_at),
        )
        conn.execute("RELEASE review_policy_bootstrap")
    except BaseException:
        with suppress(BaseException):
            conn.execute("ROLLBACK TO review_policy_bootstrap")
            conn.execute("RELEASE review_policy_bootstrap")
        raise
