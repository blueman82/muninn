"""Explain retained learning outcomes without exposing source content."""

from __future__ import annotations

import json
import sqlite3

from muninn import review_recovery, review_state
from muninn.knowledge_models import RequestId
from muninn.review_diagnostics import correction_status
from muninn.review_models import CorrectionStatus
from muninn.review_policy_status import policy_status
from muninn.review_request_views import request_row, request_view

_STAGES = {
    "native output": "while checking the native runner output",
    "model format": "while checking the model result format",
    "candidate validation": "while validating proposed candidates",
    "controlled read": "while performing a controlled read",
    "original evidence": "while checking original source evidence",
    "commit": "while committing the checked result",
    "evidence": "while preparing and checking source evidence",
    "prompt": "while preparing the model input",
    "launch": "while preparing to launch the model",
    "review": "during the review; the finer failure phase was not recorded",
    "runner": "while running the model",
    "recovery": "while checking an earlier process",
    "runner setup file": "while checking the runner setup file",
    "review instructions": "while checking review instructions",
}
_EXPECTED = frozenset(
    {"rejected_authority", "rejected_scope", "rejected_erased"}
)
_SOURCE_LIMITS = frozenset(
    {
        "evidence_limit",
        "evidence_byte_limit",
        "evidence_record_limit",
        "evidence_work_limit",
        "evidence_selection_limit",
    }
)


def _saved(conn: sqlite3.Connection, ident: str) -> sqlite3.Row | None:
    """Read a saved notice without initializing optional state."""
    if not review_state.available(conn, "review_issue"):
        return None
    return conn.execute(
        "SELECT * FROM review_issue WHERE id=?", (ident,)
    ).fetchone()


def _failure(conn: sqlite3.Connection, ident: str) -> tuple[str, str]:
    """Distinguish saved notices from incomplete historical attempt data."""
    saved = _saved(conn, ident)
    if saved is not None:
        code, stage = saved["code"], saved["stage"]
        origin = "A saved failure notice remains for this request."
    else:
        attempt = conn.execute(
            "SELECT error_code FROM review_attempt WHERE request_id=? "
            "AND error_code IS NOT NULL ORDER BY started_at DESC,id DESC "
            "LIMIT 1",
            (ident,),
        ).fetchone()
        if attempt is None:
            return "", "No failure is recorded for this request."
        code, stage = attempt[0], "not recorded"
        origin = (
            "This is a historical attempt failure; no saved request notice "
            "remains. Its failure stage was not recorded."
        )
    detail = review_recovery.explain(code, stage)
    phase = _STAGES.get(stage, "at a stage that was not recorded")
    if stage == "review" and code in _EXPECTED.union({"rejected_validation"}):
        phase = (
            "while validating a proposed result; "
            "the finer failure phase was not recorded"
        )
    return detail["code"], (
        f"{origin} The failure occurred {phase}. "
        f"Check: {detail['code']}. {detail['cause']}"
    )


def _impact(conn: sqlite3.Connection, ident: str) -> str:
    """Use the atomic receipt rather than infer partial proposal writes."""
    receipt = conn.execute(
        "SELECT applied_json,unresolved_json,outcome FROM review_receipt "
        "WHERE request_id=?",
        (ident,),
    ).fetchone()
    if receipt is None:
        return (
            "Nothing from this request was saved as knowledge: "
            "there is no commit receipt. Proposed batches are applied "
            "atomically, so a rejected batch saves no partial changes."
        )
    applied = len(json.loads(receipt[0]))
    unresolved = len(json.loads(receipt[1]))
    return (
        f"The commit receipt records {applied} applied knowledge changes "
        f"and {unresolved} unresolved items. Receipt outcome: {receipt[2]}. "
        "This is the recorded commit "
        "outcome; later changes or erasure can affect current knowledge."
    )


def _evidence(conn: sqlite3.Connection, ident: str) -> str:
    """Report retained evidence availability without copying any excerpt."""
    row = conn.execute(
        "SELECT count(*),coalesce(sum(e.state='live' AND "
        "e.excerpt IS NOT NULL),0) FROM review_input i JOIN "
        "review_evidence e ON e.id=i.evidence_id WHERE i.request_id=?",
        (ident,),
    ).fetchone()
    return (
        "The original proposed sentence is unavailable in this inspection. "
        "Failed model proposals are not retained. "
        f"The request has {row[0]} evidence records; {row[1]} have active "
        "retained excerpts. The exact passage cited by a rejected proposal "
        "is unavailable because that proposal was not retained. "
        "This explanation displays no transcript text. "
        f"Use muninn learn show {ident} to explicitly inspect governed "
        "evidence records; an excerpt can be unavailable or erased."
    )


def _calls(conn: sqlite3.Connection, ident: str) -> str:
    """Preserve the distinction between absent records and absent calls."""
    count = conn.execute(
        "SELECT count(*) FROM review_call c JOIN review_attempt a "
        "ON a.id=c.attempt_id WHERE a.request_id=? "
        "AND c.launched_at IS NOT NULL",
        (ident,),
    ).fetchone()[0]
    if count:
        return f"Model launches recorded for this request: {count}."
    return (
        "No model launch is recorded for this request. "
        "Missing launch records do not prove that no model call occurred."
    )


def _correction(status: CorrectionStatus) -> str:
    """Describe correction evidence separately from the saved commit impact."""
    meanings = {
        "unknown": (
            "Retained history does not prove a correction was requested."
        ),
        "requested": (
            "A correction was requested; no exact successor launch is "
            "recorded."
        ),
        "attempted": (
            "The exact successor launched, but no correction result is "
            "established."
        ),
        "succeeded": (
            "The completed successor has a receipt from the same attempt."
        ),
        "exhausted": (
            "The completed successor ended in a failure of the same "
            "attempt."
        ),
    }
    return (
        f"Correction status: {status}. {meanings[status]} "
        "This status describes the latest attempt with a "
        "correction marker. "
        "A later ordinary success does not prove an earlier "
        "correction succeeded. "
        "The receipt separately records saved changes or no change. "
        "Clearing a failure notice preserves durable correction "
        "markers."
    )


def _learning(conn: sqlite3.Connection) -> str:
    """Describe current admission separately from a historical failure."""
    status = policy_status(conn)
    setup = _saved(conn, "setup")
    if not status.enabled or status.policy_status != "valid":
        current = (
            "Current learning admission is paused or its policy cannot be "
            "verified. Inspect muninn doctor --pretty before changing setup. "
            "A historical request failure does not identify the current cause."
        )
    else:
        current = (
            "Current learning admission is enabled with a valid stored "
            "policy. Unrelated requests can continue subject to current "
            "runtime checks. This does not prove a worker is running "
            "or that current setup passes."
        )
    if setup is not None:
        detail = review_recovery.explain(setup["code"], setup["stage"])
        current += (
            " "
            "A separate saved setup notice takes priority over request "
            f"notices: {detail['cause']} {detail['action']} "
            "This saved notice alone does not prove the setup still fails."
        )
    return current


def _action(code: str, ident: str, state: str) -> str:
    """Give an exact supported action without recommending blind retries."""
    inspect = f"muninn learn show {ident} --pretty"
    if code in _SOURCE_LIMITS:
        return (
            "The Muninn maintainer must inspect the source-read allowance. "
            f"Provide the safe failure code from {inspect}. "
            "Do not retry this deterministic limit failure unchanged. "
            "Source-read work differs from memory capacity or model context. "
            "A narrower window may still require reading earlier records. "
            "No historical limit or measured counter is inferred here."
        )
    if code == "config":
        return (
            "The specific cause was not recorded. Inspect muninn doctor "
            "--pretty for current checks and give its safe diagnostics to "
            "the Muninn maintainer. The old stage cannot recover the cause. "
            "Do not retry solely to recover discarded diagnostics."
        )
    if code in _EXPECTED:
        return (
            "A safety check rejected this proposal's support. Rejection is "
            "expected when owner evidence is unverified, scope is invalid, "
            "or evidence was erased. If genuine direct owner wording was "
            "rejected incorrectly, the Muninn maintainer must investigate. "
            f"Inspect {inspect} to examine its support. "
            "Do not relabel the proposal or retry unchanged to bypass the "
            "check. You can dismiss its reminder after inspection."
        )
    if review_recovery.shared(code):
        return (
            "The local operator can restore the approved setup. "
            + review_recovery.explain(code, "not recorded")["action"]
            + " Use muninn doctor --pretty to inspect current checks. "
            "Do not change the approved policy to suppress this refusal."
        )
    if code:
        return (
            f"Inspect {inspect}. If the recorded failure has no clear local "
            "repair, give its safe code to the Muninn maintainer. "
            + (
                "This request is waiting within its bounded retry policy."
                if state == "waiting"
                else "Do not retry an unchanged rejected result."
            )
        )
    return "No failure recovery action is indicated by the retained history."


def explain(conn: sqlite3.Connection, ident: RequestId) -> dict[str, str]:
    """Return ordinary-language inspection using only retained metadata."""
    request = request_view(request_row(conn, ident))
    code, failure = _failure(conn, ident)
    correction = correction_status(conn, ident)
    paragraphs = [
        f"Learning request {ident} is currently {request.state}.",
        _evidence(conn, ident),
        failure,
        _correction(correction),
        _impact(conn, ident),
        _calls(conn, ident),
        _learning(conn),
        _action(code, ident, request.state),
        f"To clear only this request's saved reminder, run muninn learn "
        f"dismiss {ident}. Dismissal preserves request history and does not "
        "retry, approve a proposal, enable learning, or fix a failure. "
        "It removes the saved stage and action; recorded attempt codes "
        "remain. Dismissing the newest notice can reveal an older one. "
        "Setup notices take priority. Upgrades preserve saved notices. "
        "Successful completion clears this request's notice. "
        "A later failure can create another notice.",
    ]
    return {
        "request_id": ident,
        "correction_status": correction,
        "explanation": "\n\n".join(paragraphs),
    }
