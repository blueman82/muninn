"""Name the finite review failure codes and explain each in one sentence."""

from __future__ import annotations

_LIMIT = (
    "Original-source verification exceeded a read limit. Do not retry this "
    "request unchanged."
)
_REJECTED = "Muninn rejected the result and continues with other requests."
EXPLANATIONS: dict[str, str] = {
    "deadline": "The review exceeded its time limit. Muninn will retry.",
    "unavailable": "The review did not finish. Muninn will retry.",
    "config": "Muninn has no safe cause for this failure. Run muninn doctor.",
    "auth": "The model program is not signed in.",
    "budget": "The review budget was used up.",
    "budget_clock_reversed": "The system clock moved backwards.",
    "source_unavailable": "The source transcript is no longer indexed.",
    "review_policy_required": "The review policy changed while it waited.",
    "incomplete_window": "The indexed source no longer holds the window.",
    "prompt_limit": "The review prompt exceeded its input byte limit.",
    "evidence_limit": _LIMIT,
    "evidence_byte_limit": _LIMIT,
    "evidence_record_limit": _LIMIT,
    "evidence_work_limit": _LIMIT,
    "evidence_selection_limit": _LIMIT,
    "rejected_authority": "The change lacks verified owner evidence. "
    + _REJECTED,
    "rejected_revision": "Saved knowledge changed during the review. "
    "Muninn will rebuild it.",
    "rejected_validation": "The result did not match the required format. "
    + _REJECTED,
    "rejected_evidence": "Source evidence is missing or changed. " + _REJECTED,
    "rejected_fenced": "Another worker took over this request.",
    "rejected_erased": "The request refers to erased evidence.",
    "rejected_scope": "The change is outside the project scope. " + _REJECTED,
}
CODES = frozenset(EXPLANATIONS)


def explain(code: str) -> str:
    """Return the fixed sentence for a code, never echoing other text."""
    return EXPLANATIONS.get(code, EXPLANATIONS["config"])
