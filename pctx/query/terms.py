"""Parse REFs and turn free text into an FTS5 MATCH string."""

from __future__ import annotations

import re

from pctx.query.constants import PROVIDERS

MAX_TERMS = 16
MAX_PHRASES = 8
MAX_PHRASE_PARTS = 12  # words in one phrase; a longer one keeps its start
MAX_TERM_LEN = 100  # a longer "word" is a blob, not something to search for

_REF = re.compile(r"(\w+):(\S+):(\d+)(?:\.(\d+))?")
_QUOTED = re.compile(r'"([^"]*)"')
_WORD = re.compile(r"\w+")
_PART = re.compile(r"[^\W_]+")  # what FTS5's unicode61 counts as a token
# Linear on purpose: the separator class cannot overlap the word class, so a
# long run of punctuation cannot cause catastrophic backtracking.
_IDENT = re.compile(r"\w+(?:[./\\:-]+\w+)*")
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "have",
        "how",
        "if",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "not",
        "that",
        "the",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "you",
    }
)


def parse_ref(ref: str) -> tuple[str, str, int, int]:
    """Split a REF into provider, thread id (or prefix), line and part.

    A REF is ``provider:thread_id:line.part``; the part defaults to 1. A
    thread prefix is resolved against the store when the event is looked up.

    Args:
        ref: The REF text, surrounding whitespace allowed.

    Returns:
        The provider, the thread id or a prefix of it, the line and the part.

    Raises:
        ValueError: If the text is not a REF or a number is out of range.
    """
    found = _REF.fullmatch(ref.strip())
    if not found or found[1] not in PROVIDERS:
        raise ValueError("not a REF (provider:thread_id:line.part)")
    line, part = int(found[3]), int(found[4] or 1)
    # Far above any real transcript line; rejects absurd numbers early.
    if not (0 < line < 2**31 and 0 < part < 2**31):
        raise ValueError("line and part are 1-based integers")
    return found[1], found[2], line, part


def build_fts_query(text: str) -> str | None:
    """Build an FTS5 MATCH string: quoted terms joined by OR.

    Terms are lower-case words (letters, digits, underscore) minus
    stopwords, 1-char words and blobs over MAX_TERM_LEN, deduped, at most
    MAX_TERMS. A "quoted phrase" stays a phrase (in place of its words); an
    identifier such as ``hook_core.py`` also adds a phrase of its parts.
    Every element is double-quoted, so no FTS operator survives. Linear in
    the text length.

    Args:
        text: The user's search text.

    Returns:
        The MATCH string, or None when the text has no searchable terms.
    """
    phrases: list[str] = []

    def phrase(text: str) -> str:
        parts = _PART.findall(text)
        if len(parts) > 1:
            phrases.append(" ".join(parts[:MAX_PHRASE_PARTS]))
        return " " if len(parts) > 1 else f" {text} "

    rest = _QUOTED.sub(lambda found: phrase(found[1]), text.lower())
    for ident in _IDENT.findall(rest):
        phrase(ident)
    words = dict.fromkeys(
        w
        for w in _WORD.findall(rest)
        if 1 < len(w) <= MAX_TERM_LEN
        and w not in STOPWORDS
        and _PART.search(w)
    )
    elements = list(words)[:MAX_TERMS]
    elements += list(dict.fromkeys(phrases))[:MAX_PHRASES]
    return " OR ".join(f'"{e}"' for e in elements) or None
