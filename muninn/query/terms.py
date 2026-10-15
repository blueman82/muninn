"""Parse REFs and turn free text into an FTS5 MATCH string."""

from __future__ import annotations

import re
import sqlite3

from muninn.query.constants import PROVIDERS

MAX_TERMS = 16
MAX_PHRASES = 8
MAX_PHRASE_PARTS = 12  # words in one phrase; a longer one keeps its start
MIN_EVENTS = 1000  # below this a df rule is noise; the list rule applies
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


def _extract(text: str) -> tuple[list[str], list[str]]:
    """Split text into its deduped single words and its phrases.

    Args:
        text: The user's search text.

    Returns:
        The lower-case words (1 < length <= MAX_TERM_LEN, no stopword
        filtering, no cap) and the phrases, in order.
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
        if 1 < len(w) <= MAX_TERM_LEN and _PART.search(w)
    )
    return list(words), list(dict.fromkeys(phrases))


def _match(words: list[str], phrases: list[str]) -> str | None:
    """Join the capped words and phrases into a MATCH string, or None."""
    elements = words[:MAX_TERMS] + phrases[:MAX_PHRASES]
    return " OR ".join(f'"{e}"' for e in elements) or None


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
    words, phrases = _extract(text)
    return _match([w for w in words if w not in STOPWORDS], phrases)


def parse_rule(rule: str) -> float | None:
    """Validate a term rule: ``list``, ``none`` or ``df:R`` with 0 < R <= 1.

    Args:
        rule: The rule text.

    Returns:
        The ratio R for a df rule, None for ``list`` and ``none``.

    Raises:
        ValueError: If the rule is anything else.
    """
    if rule in ("list", "none"):
        return None
    ratio = float(rule[3:]) if rule.startswith("df:") else 0.0
    if not 0 < ratio <= 1:
        raise ValueError("terms must be list, none or df:R (0 < R <= 1)")
    return ratio


def _doc_freq(conn: sqlite3.Connection, word: str) -> int:
    """Count the events whose text holds word (a MATCH count, read-only)."""
    sql = "SELECT count(*) FROM event_fts WHERE event_fts MATCH ?"
    return int(conn.execute(sql, (f'"{word}"',)).fetchone()[0])


def fts_terms(
    conn: sqlite3.Connection, text: str, rule: str = "list"
) -> tuple[str | None, list[str]]:
    """Build the MATCH string and the kept single words under a term rule.

    ``list`` is ``build_fts_query``; ``none`` applies no stopword list;
    ``df:R`` drops a single word held by more than R of all events, and
    keeps the rarest word when that would drop them all. Phrases are never
    filtered. A store under MIN_EVENTS events uses the list rule for df.

    Args:
        conn: Read-only store connection.
        text: The user's search text.
        rule: ``list``, ``none`` or ``df:R``.

    Returns:
        The MATCH string (None when nothing is searchable) and the kept
        single words, capped at MAX_TERMS.

    Raises:
        ValueError: If the rule is invalid.
    """
    ratio = parse_rule(rule)
    words, phrases = _extract(text)
    kept = [w for w in words if rule == "none" or w not in STOPWORDS]
    if ratio is not None and words:
        total = conn.execute("SELECT count(*) FROM event_fts_docsize")
        events = int(total.fetchone()[0])
        # ponytail: one MATCH count per word; cap the words if a hook uses it.
        if events >= MIN_EVENTS:
            freq = {w: _doc_freq(conn, w) for w in words}
            kept = [w for w in words if freq[w] <= ratio * events]
            kept = kept or [min(words, key=lambda w: freq[w])]
    return _match(kept, phrases), kept[:MAX_TERMS]
