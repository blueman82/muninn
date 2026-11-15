"""Explain the owner reply to a display without granting authority."""

from __future__ import annotations


def owner_instructions(ident: str) -> dict[str, str]:
    """Explain the complete display and the owner's own-words reply."""
    return {
        "meaning": "This proposal is the complete change identified by "
        f"{ident}. Viewing it does not approve it.",
        "reply": "Reply in your own words straight after this display. "
        "Say whether to approve it, reject it, or change it.",
        "edit": "To change it, ask the agent to prepare and show a new "
        "complete proposal. The new proposal needs its own reply.",
        "agent": "The agent reads your reply and records approve, reject "
        "or edit. Muninn checks that you sent the reply directly after "
        "this display. Your reply alone does not mean the memory was saved.",
    }
