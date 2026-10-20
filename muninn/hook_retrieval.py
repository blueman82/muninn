"""Static prompt guidance for advisory retrieval mode."""

from __future__ import annotations

from muninn.hook_context import as_payload

RECALL_ADVISORY = "recall.advisory"
RETRIEVAL_HINT = (
    "<muninn-retrieval>\n"
    'For project context/knowledge retrieval run `muninn search "<terms>"`;'
    " refine or widen if needed. For owner decisions, start with"
    " `--kind prompt`; for assistant reports, use `--kind reply`.\n"
    "open retrievals with `muninn open REF --context 3`. For K_ IDs,"
    " use `muninn know show K_ID` and open cited originals; E_ IDs are"
    " not open refs. Previews only help you navigate—support past claims"
    " with opened originals.\n"
    "If history doesn't matter, skip. If relevant evidence is missing,"
    " say unknown.\n"
    "</muninn-retrieval>"
)


def advisory_output(payload: object) -> dict[str, object]:
    """Return guidance for an ordinary prompt without opening the store."""
    prompt = as_payload(payload).get("prompt")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or prompt.lstrip().startswith("/")
    ):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": RETRIEVAL_HINT,
        }
    }
