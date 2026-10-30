"""Explain finite review failures without exposing model or transcript text."""

from __future__ import annotations

import sqlite3

from muninn import review_state
from muninn.review_models import ReviewPolicy
from muninn.review_policy_status import policy_status
from muninn.review_recovery_codes import (
    CONTAINED,
    EXPLANATIONS,
    SHARED,
    STARTUP_STAGES,
)


def enabled(policy: ReviewPolicy) -> bool:
    """Limit widened ongoing recovery authority to its approved revision."""
    return policy.policy_id == "muninn-codex-ongoing-production-v3"


def explain(code: str, stage: str) -> dict[str, str]:
    """Return fixed cause and action text without echoing arbitrary errors."""
    safe = code if code in EXPLANATIONS else "config"
    cause, action = EXPLANATIONS[safe]
    return {
        "code": safe,
        "stage": stage if stage in STARTUP_STAGES else "not recorded",
        "cause": cause,
        "action": action,
    }


def shared(code: str) -> bool:
    """Recognise setup failures whose recheck must precede further calls."""
    return code in SHARED


def _detail(
    conn: sqlite3.Connection, ident: str, code: str, stage: str
) -> dict[str, str]:
    """Render safe current wording and bounded request state from codes."""
    detail = explain(code, stage)
    state = conn.execute(
        "SELECT state FROM review_request WHERE id=?", (ident,)
    ).fetchone()
    if (
        state is not None
        and state[0] == "failed"
        and code in {"deadline", "unavailable", "rejected_revision"}
    ):
        count = conn.execute(
            "SELECT count(*) FROM review_attempt WHERE request_id=?", (ident,)
        ).fetchone()[0]
        detail["action"] = (
            f"Muninn stopped this request after {count} attempts. "
            f"Other requests can continue."
        )
    control = conn.execute("SELECT admission FROM review_control").fetchone()
    if ident != "setup" and not control[0]:
        detail["action"] = (
            "Learning remains paused under the installed policy. "
            "Inspect this failure before enabling learning."
        )
    if ident == "setup" and code == "config":
        detail["action"] = (
            "The background program can check again while running and "
            "learning is enabled. "
            "Reading this notice starts no timer."
        )
    return detail


def record(
    conn: sqlite3.Connection, ident: str, code: str, stage: str, now: float
) -> None:
    """Save only program-owned diagnostics under the caller's writer root."""
    review_state.ensure(conn)
    detail = _detail(conn, ident, code, stage)
    conn.execute(
        "INSERT OR REPLACE INTO review_issue VALUES(?,?,?,?,?,?)",
        (
            ident,
            detail["code"],
            detail["stage"],
            detail["cause"],
            detail["action"],
            now,
        ),
    )


def clear(conn: sqlite3.Connection, ident: str) -> bool:
    """Remove one saved notice without changing learning state."""
    if review_state.available(conn, "review_issue"):
        return (
            conn.execute(
                "DELETE FROM review_issue WHERE id=?", (ident,)
            ).rowcount
            > 0
        )
    return False


def notice(conn: sqlite3.Connection) -> dict[str, str] | None:
    """Return one unresolved setup or recent request failure for people."""
    if not review_state.available(conn, "review_issue"):
        return None
    row = conn.execute(
        "SELECT * FROM review_issue ORDER BY id='setup' "
        "DESC,created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        **_detail(conn, row["id"], row["code"], row["stage"]),
    }


def startup_action(conn: sqlite3.Connection) -> dict[str, str] | None:
    """Select an owner action using saved admission and verified policy state.

    This read does not establish live host or account health and changes no
    notices, requests or admission. Unknown codes remain eligible.

    Args:
        conn: Existing connection whose read snapshot is caller-owned.

    Returns:
        One fixed action notice, or None when no owner action is required.
    """
    status = policy_status(conn)
    safe = status.enabled is True and status.policy_status == "valid"
    pause = (
        "Keep learning paused. Run muninn doctor --pretty for current checks."
    )
    if review_state.available(conn, "review_issue"):
        rows = conn.execute(
            "SELECT id,code,stage FROM review_issue "
            "ORDER BY id='setup' DESC,created_at DESC"
        )
        issue: sqlite3.Row | None = None
        for row in rows:
            if row["id"] != "setup" and row["code"] in CONTAINED:
                if not safe and issue is None:
                    issue = row
                continue
            issue = row
            break
        if issue is not None:
            stage = (
                issue["stage"]
                if issue["stage"] in STARTUP_STAGES
                else "not recorded"
            )
            detail = {"id": issue["id"], **explain(issue["code"], stage)}
            if not safe:
                detail["action"] = (
                    pause
                    if issue["code"] in CONTAINED
                    else detail["action"] + " " + pause
                )
            detail["admission"] = "open" if safe else "closed"
            return detail
    if safe:
        return None
    return {
        "id": "setup",
        "code": (
            "paused"
            if status.policy_status == "valid"
            else status.policy_status
        ),
        "stage": "admission",
        "cause": (
            "Learning admission is closed."
            if status.enabled is False
            else "Muninn cannot verify saved learning admission."
        ),
        "action": pause,
    }


def details(conn: sqlite3.Connection, request_id: str) -> list[dict[str, str]]:
    """Explain request failures and identify missing historical detail."""
    if review_state.available(conn, "review_issue"):
        rows = conn.execute(
            "SELECT * FROM review_issue WHERE id=?", (request_id,)
        ).fetchall()
        if rows:
            return [
                {
                    "id": row["id"],
                    **_detail(conn, row["id"], row["code"], row["stage"]),
                }
                for row in rows
            ]
    return [
        explain(row[0], "not recorded")
        for row in conn.execute(
            "SELECT error_code FROM review_attempt WHERE "
            "request_id=? AND error_code IS NOT NULL ORDER BY started_at DESC",
            (request_id,),
        )
    ]


def health_checks(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, bool, str, str], ...]:
    """Return optional safe health detail from the same session notice."""
    issue = notice(conn)
    if issue is None:
        return ()
    message = issue["stage"] + ": " + issue["cause"] + " " + issue["action"]
    return (("review_attention", False, message, "warn"),)
