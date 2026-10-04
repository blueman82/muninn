"""Shared vocabulary of the knowledge ledger: limits, refusals, text rules.

Everything here is used by more than one of the knowledge modules, which is
why it lives below them and imports none of them.
"""

from __future__ import annotations

import re
import time
from typing import Any

from muninn import classify

type Entry = dict[str, Any]
type Cite = dict[str, Any]

KINDS = (
    "decision",
    "fact",
    "preference",
    "procedure",
    "lesson",
    "constraint",
)
CITABLE = ("prompt", "reply", "tool_call")  # of a primary thread, unflagged
STATUSES = ("current", "superseded", "retracted", "erased")
TEXT_MAX = 500
REASON_MAX = 200
QUOTE_MIN = 12
QUOTE_MAX = 300
CALLER_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID")
CHAIN_MAX = 100  # hops followed along a supersede chain
PROBLEMS_MAX = 100  # broken citations named by check()
BLOCK_QUOTE = 120  # characters of a quote pushed in the SessionStart block
# The citation rows (alias m) that let an entry be pushed into a prompt or a
# session: live, and of a user prompt. Anything else stays pull-only, because
# only text the person typed is trusted enough to be injected unprompted.
PUSHABLE_CITE = "m.state = 'live' AND m.role = 'user' AND m.kind = 'prompt'"

PULL_ONLY = (
    "pull-only: not pushed into session memory because no cited user prompt"
    " holds this text; cite a user prompt, or re-add it in the user's own"
    " words"
)
PUSH_OVERLAP = 0.8  # share of an entry's words its user quote must hold

# The ``<`` of a muninn frame delimiter, however spaced or cased. Escaping it
# stops stored text from closing or opening a memory block when it is
# rendered back into a prompt.
_FRAME = re.compile(r"(?i)<(?=\s*/?\s*muninn-(?:memory|recall))")


class RefusedError(Exception):
    """A write was refused and nothing was written (exit 2).

    Attributes:
        code: One of bad_kind, bad_actor, text_length, reason_length,
            uncited, bad_ref, not_found, ambiguous_ref, not_citable,
            quote_length, quote_not_found, approval_needs_reply,
            no_caller_session, preference_needs_user, bad_supersedes,
            not_current, bad_confidence, bad_sensitivity, bad_valid_until,
            bad_tags, bad_contradicts or bad_loop_scope.
        detail: Optional extra context for the code.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        """Build the refusal; the message is ``code`` or ``code: detail``.

        Args:
            code: Machine-readable refusal code.
            detail: Optional context appended to the message.
        """
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def clean_text(text: str, code: str, low: int, high: int) -> str:
    """Return ``text`` stripped, redacted and frame-escaped.

    Args:
        text: Caller-supplied text.
        code: Refusal code used when the length is out of range.
        low: Minimum length in characters, after cleaning.
        high: Maximum length in characters, after cleaning.

    Returns:
        The cleaned text.

    Raises:
        RefusedError: If the text is longer than ``4 * high`` or its
            cleaned length is not within ``low..high``.
    """
    if len(text) > 4 * high:  # not worth scanning
        raise RefusedError(code)
    body = _FRAME.sub("&lt;", classify.redact(text.strip())[0])
    if not low <= len(body) <= high:
        raise RefusedError(code)
    return body


def parse_kid(value: object) -> int | None:
    """Return the entry id in ``12``, ``"12"`` or ``"K12"``, else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    found = re.fullmatch(r"[Kk]?([0-9]{1,18})", str(value or "").strip())
    return int(found[1]) or None if found else None


def entry_name(kid: int | None) -> str | None:
    """Return the public name (``K12``) of an entry id."""
    return None if kid is None else f"K{kid}"


def iso_date(at: float) -> str:
    """Return the UTC calendar date of a timestamp."""
    return time.strftime("%Y-%m-%d", time.gmtime(at))


def text_backed(text: str, quote: str) -> bool:
    """Say whether a user's quote backs the text that would be pushed.

    The pushed text is written by an agent; the quote is what the person
    typed. Pushing is trusted only when the text is inside the quote or
    mostly made of its words, so unrelated text cannot ride on a cite.

    Args:
        text: The entry text.
        quote: A cited user-prompt quote.

    Returns:
        True if the text is a substring of the quote (whitespace collapsed,
        case folded) or at least ``PUSH_OVERLAP`` of its words of three or
        more characters occur in the quote.
    """
    body = " ".join(text.split()).casefold()
    said = " ".join(quote.split()).casefold()
    if not body:
        return False
    if body in said:
        return True
    words = [w for w in re.findall(r"\w+", body) if len(w) >= 3]
    heard = set(re.findall(r"\w+", said))
    if not words:
        return False
    return sum(w in heard for w in words) / len(words) >= PUSH_OVERLAP
