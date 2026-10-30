"""Format the exact whole-message owner choice without granting authority."""

from __future__ import annotations

from typing import Literal


def choice_words(
    ident: str,
    action: Literal["approve", "reject", "edit"],
    replacement: str | None = None,
) -> str:
    """Return the exact supported sentence for the supplied identities."""
    if action == "edit":
        return f"Please replace memory {ident} with memory {replacement}."
    return f"Please {action} memory {ident}."


def owner_instructions(ident: str) -> dict[str, str]:
    """Explain the complete display and its separate plain-chat choice."""
    return {
        "meaning": "This proposal is the complete change identified by "
        f"{ident}. Viewing it does not approve it.",
        "approve": choice_words(ident, "approve"),
        "reject": choice_words(ident, "reject"),
        "edit": "Ask the agent to prepare and show a new complete proposal. "
        "The new proposal needs its own exact approval.",
        "reply": "Send the complete approve or reject sentence as your "
        "plain chat message. Keep its case, spaces, and final period.",
        "agent": "The agent handles all commands and checks. "
        "Your reply alone does not mean the memory was saved.",
    }
