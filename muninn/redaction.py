"""Secret redaction for text that is about to be stored or shown.

The patterns here are the privacy boundary of the whole store: every event
text and knowledge entry text passes through ``redact`` before it is
written. Do not edit a pattern without re-running the hostile-input tests.
"""

from __future__ import annotations

import re

REDACTED = "[redacted:secret]"

# Every pattern below must stay near-linear on hostile text: transcripts are
# untrusted, and a catastrophic backtrack here would stall whichever caller
# redacts, including ingest. Repeats are therefore bounded ({0,16384}),
# tempered (the (?!-----BEGIN ) guard) or limited to horizontal whitespace.
# The remaining unbounded repeats (\S+, \s*, the base64 run) are not
# ambiguous about where one repeat ends and the next begins; the one nested
# repeat, the base64 lines of a truncated key, is safe only because each
# iteration must start at a newline that the base64 class excludes. The canary
# test feeds hostile text through redact; re-check this when adding a pattern.
_URL = r"(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://"
_KEYS = (
    r"api[_-]?key|access[_-]?token|client[_-]?secret|token|"
    r"bearer|password|passwd|secret[_-]?access[_-]?key|"
    r"secret|private[_-]?key"
)
# A token must not begin in the middle of an identifier or word: "disk_live_"
# holds "sk_live_" and "task-" holds "sk-", and neither is a key.
_EDGE = r"(?<![A-Za-z0-9])"
# Provider tokens with a fixed prefix; each needs a long enough tail that
# ordinary words and short identifiers do not match.
_VENDOR = (
    _EDGE + r"(?:xox[abprs]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{35}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|whsec_[A-Za-z0-9]{16,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|npm_[A-Za-z0-9]{36}|hf_[A-Za-z0-9]{30,}|ya29\.[A-Za-z0-9_-]{20,}"
    r"|dop_v1_[A-Za-z0-9]{40,})"
)
_JWT = r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
# A key block's header and footer, including "PGP PRIVATE KEY BLOCK".
_PEM_TAG = r"(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"

# Group "v" marks the secret value to blank out; a pattern without it
# redacts its whole match. A separate pattern for secrets in URL query
# strings is deliberately absent: every such span already lies inside a
# key=value match of the first pattern or the auth one.
SECRET_PATTERNS = (
    re.compile(rf"(?i)(?:{_KEYS})\s*(?:=|:)\s*(?P<v>\S+)"),
    re.compile(r"(?i)bearer\s+(?P<v>\S+)"),
    re.compile(
        rf"(?i){_EDGE}(?:sk-[A-Za-z0-9_-]{{20,}}|gh[pousr]_[A-Za-z0-9_]{{20,}}"
        r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})"
    ),
    re.compile(_VENDOR),
    re.compile(_JWT),
    # An auth header's value, except "Basic" before an ordinary word (prose
    # such as "authorization: basic configuration"); a real Basic header is
    # taken whole by the next pattern.  The inline flags make the word test
    # case-sensitive: base64 mixes cases inside a word, prose does not.
    re.compile(
        r"(?i)auth(?:orization)?\s*[=:]\s*"
        r"(?!basic[ \t]+(?-i:[A-Za-z][a-z]*)(?![A-Za-z0-9+/=]))(?P<v>\S+)"
    ),
    # The credential after "Basic": it must look like base64 (a digit, a
    # sign, padding or a case change inside the run), so a plain word is not
    # taken for one.
    re.compile(
        r"(?i)auth(?:orization)?[ \t]*[:=][ \t]*(?P<v>basic[ \t]+"
        r"(?=[A-Za-z0-9+/]*(?:[0-9+/=]|(?-i:[a-z][A-Z])))"
        r"[A-Za-z0-9+/=]{8,})"
    ),
    re.compile(rf"(?i){_URL}(?P<v>[^/\s@]+)@\S+"),
    # The optional backslashes cover JSON nested inside a JSON string, where
    # every quote arrives escaped. A backslash not followed by a quote is
    # part of the value, so the value loop's two branches never overlap.
    re.compile(
        r"(?i)\\?[\"'](?:api[_-]?key|access[_-]?token|client[_-]?secret|"
        r"token|auth(?:orization)?|password|passwd|"
        r"secret[_-]?access[_-]?key|secret|private[_-]?key)\\?[\"']"
        r"\s*:\s*\\?[\"'](?P<v>(?:[^\"'\\]|\\(?![\"']))+)\\?[\"']"
    ),
    re.compile(
        r"(?is)(?:api[_-]?key|access[_-]?token|client[_-]?secret|token|"
        r"password|passwd|secret)[ \t]*\r?\n\s*[:=]\s*(?P<v>\S+)"
    ),
    # A private-key block whose END line was cut off (truncated output) still
    # loses the base64 lines that follow its BEGIN line.
    re.compile(
        rf"-----BEGIN {_PEM_TAG}"
        r"(?:(?:(?!-----BEGIN ).){0,16384}?"
        rf"-----END {_PEM_TAG}"
        r"|(?:(?:\r?\n|\\n)[A-Za-z0-9+/=]+)*)",
        re.S,
    ),
)


def redact(text: str) -> tuple[str, bool]:
    """Replace secret spans with ``REDACTED``.

    Spans from all patterns are merged first so that overlapping matches
    produce one marker, not several.

    Args:
        text: Text that may contain secrets.

    Returns:
        The redacted text and whether it differs from ``text``.
    """
    spans: list[tuple[int, int]] = []
    for pattern in SECRET_PATTERNS:
        group = "v" if "v" in pattern.groupindex else 0
        spans.extend(m.span(group) for m in pattern.finditer(text))
    if not spans:
        return text, False
    pieces: list[str] = []
    pos = 0
    for start, end in sorted(spans):
        if start >= pos:
            pieces += [text[pos:start], REDACTED]
            pos = end
        elif end > pos:  # overlaps the previous span: extend it
            pos = end
    pieces.append(text[pos:])
    new = "".join(pieces)
    return new, new != text
