"""Explain finite review failures without exposing model or transcript text."""

from __future__ import annotations

import sqlite3

from muninn import review_state
from muninn.review_models import ReviewPolicy
from muninn.review_policy_status import policy_status

_EXPLANATIONS: dict[str, tuple[str, str]] = {
    "deadline": (
        "The review exceeded its time limit.",
        "Muninn will retry within the request limit.",
    ),
    "unavailable": (
        "The review runner did not finish successfully.",
        "Muninn will retry within the request limit.",
    ),
    "config": (
        "Muninn could not verify the review setup. "
        "The specific cause was not recorded.",
        "Run muninn doctor --pretty for current setup checks. "
        "Give unresolved safe diagnostics to the Muninn maintainer.",
    ),
    "evidence_limit": (
        "Original-source verification exceeded its byte, record, "
        "or database-work limit.",
        "Do not retry this request unchanged. "
        "Ask the Muninn maintainer to inspect its source-read budget. "
        "A narrower window can still require reading earlier records.",
    ),
    "auth": (
        "Codex sign-in is unavailable.",
        "Open Codex and sign in. Muninn will check again.",
    ),
    "capability_changed": (
        "The installed Codex capability no longer matches its approved setup.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "capability_unverified": (
        "The runner capability has no current verification.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "capability_config": (
        "The runner configuration differs from its approved settings.",
        "Restore the approved settings. Muninn will check again.",
    ),
    "effective_host_changed": (
        "The Codex setup differs from its approved settings.",
        "Restore the approved Codex settings. Muninn will check again.",
    ),
    "launch_input_unavailable": (
        "A required review capability or prompt file is unavailable.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "launch_input_binding": (
        "A review capability or prompt does not match the installed policy.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "launch_input_invalid": (
        "A required review setup file is invalid.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "rejected_authority": (
        "The proposed change lacks verified owner evidence.",
        "Muninn rejected this result and will continue with other requests.",
    ),
    "rejected_revision": (
        "Saved knowledge changed while this review ran.",
        "Muninn will rebuild the review within the request limit.",
    ),
    "rejected_validation": (
        "The model result did not match the required format.",
        "Muninn rejected this result and will continue with other requests.",
    ),
    "rejected_evidence": (
        "Source evidence is missing, changed, or unverifiable.",
        "Muninn excluded this request. Verified requests can continue.",
    ),
    "rejected_erased": (
        "The request refers to erased evidence.",
        "Muninn excluded this request. Verified requests can continue.",
    ),
    "rejected_scope": (
        "The proposed change is outside the admitted project scope.",
        "Muninn rejected this result and will continue with other requests.",
    ),
    "native_tool_event": (
        "Codex reported a tool action during a review.",
        "Inspect the Codex activity before enabling learning again.",
    ),
    "process_identity_unverified": (
        "Muninn cannot verify whether an earlier review process stopped.",
        "Keep learning paused until Muninn can verify the process.",
    ),
}
_EXPLANATIONS.update(
    {
        f"evidence_{kind}_limit": (
            f"Original-source verification exceeded its {allowance} limit.",
            _EXPLANATIONS["evidence_limit"][1],
        )
        for kind, allowance in (
            ("byte", "physical byte-read"),
            ("record", "physical record-read"),
            ("work", "database-work"),
            ("selection", "accumulated selected-content"),
        )
    }
)
_SETUP_FAILURES = {
    "capability_unavailable": (
        "Muninn cannot read a required runner executable, "
        "proof, or schema file."
    ),
    "capability_invalid": (
        "A runner capability or proof file has an invalid format."
    ),
    "capability_path": (
        "A runner path is relative or resolves to a different path."
    ),
    "capability_storage": (
        "A runner setup file or directory fails "
        "its ownership or access checks."
    ),
    "capability_executable_storage": (
        "The runner executable fails its ownership or access checks."
    ),
    "capability_executable_provenance": (
        "Muninn cannot verify the runner executable's origin."
    ),
    "capability_platform": (
        "The runner is unavailable on this operating system or processor."
    ),
    "capability_limit": (
        "The runner setup exceeds a file size or scan limit."
    ),
    "codex_experimental_boundary": (
        "The Codex host, policy, or project does not match the approved setup."
    ),
    "native_prompt_instruction": (
        "The review instructions do not match the required Codex format."
    ),
    "launch_input_platform": (
        "This operating system cannot use the approved review setup."
    ),
    "launch_input_storage": (
        "A review setup file is outside its required private directory."
    ),
    "runner_unsupported": (
        "The selected runner has no supported launch method."
    ),
}
_EXPLANATIONS.update(
    {
        code: (
            cause,
            "Restore the approved setup. "
            "Muninn will check again in 60 seconds.",
        )
        for code, cause in _SETUP_FAILURES.items()
    }
)
_SHARED = frozenset(
    {
        "auth",
        "config",
        "effective_host_changed",
        "capability_changed",
        "capability_unverified",
        "capability_config",
        "launch_input_unavailable",
        "launch_input_binding",
        "launch_input_invalid",
    }
).union(_SETUP_FAILURES)

_CONTAINED = frozenset(
    {
        "deadline",
        "unavailable",
        "rejected_revision",
        "rejected_authority",
        "rejected_validation",
        "rejected_evidence",
        "rejected_erased",
        "rejected_scope",
        "evidence_limit",
        "evidence_byte_limit",
        "evidence_record_limit",
        "evidence_work_limit",
        "evidence_selection_limit",
    }
)
_STARTUP_STAGES = frozenset(
    {
        "native output",
        "model format",
        "candidate validation",
        "controlled read",
        "original evidence",
        "commit",
        "evidence",
        "prompt",
        "launch",
        "review",
        "runner",
        "recovery",
        "runner setup file",
        "review instructions",
    }
)


def enabled(policy: ReviewPolicy) -> bool:
    """Limit widened ongoing recovery authority to its approved revision."""
    return policy.policy_id == "muninn-codex-ongoing-production-v3"


def explain(code: str, stage: str) -> dict[str, str]:
    """Return fixed cause and action text without echoing arbitrary errors."""
    safe = code if code in _EXPLANATIONS else "config"
    cause, action = _EXPLANATIONS[safe]
    return {"code": safe, "stage": stage, "cause": cause, "action": action}


def shared(code: str) -> bool:
    """Recognise setup failures whose recheck must precede further calls."""
    return code in _SHARED


def record(
    conn: sqlite3.Connection, ident: str, code: str, stage: str, now: float
) -> None:
    """Save only program-owned diagnostics under the caller's writer root."""
    review_state.ensure(conn)
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
        detail["action"] = "Muninn will check this setup again in 60 seconds."
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
        key: row[key] for key in ("id", "code", "stage", "cause", "action")
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
            if row["id"] != "setup" and row["code"] in _CONTAINED:
                if not safe and issue is None:
                    issue = row
                continue
            issue = row
            break
        if issue is not None:
            stage = (
                issue["stage"]
                if issue["stage"] in _STARTUP_STAGES
                else "not recorded"
            )
            detail = {"id": issue["id"], **explain(issue["code"], stage)}
            if not safe:
                detail["action"] = (
                    pause
                    if issue["code"] in _CONTAINED
                    else detail["action"] + " " + pause
                )
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
                    key: row[key]
                    for key in ("id", "code", "stage", "cause", "action")
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
