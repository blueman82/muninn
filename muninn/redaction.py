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
    r"auth(?:orization)?|bearer|password|passwd|secret|private[_-]?key"
)

# Group "v" marks the secret value to blank out; a pattern without it
# redacts its whole match. A separate pattern for secrets in URL query
# strings is deliberately absent: every such span already lies inside a
# key=value match of the first pattern.
SECRET_PATTERNS = (
    re.compile(rf"(?i)(?:{_KEYS})\s*(?:=|:)\s*(?P<v>\S+)"),
    re.compile(r"(?i)bearer\s+(?P<v>\S+)"),
    re.compile(
        r"(?i)sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
    ),
    re.compile(rf"(?i){_URL}(?P<v>[^/\s@]+)@\S+"),
    re.compile(
        r"(?i)[\"'](?:api[_-]?key|access[_-]?token|client[_-]?secret|token|"
        r"auth(?:orization)?|password|passwd|secret|private[_-]?key)[\"']"
        r"\s*:\s*[\"'](?P<v>[^\"']+)[\"']"
    ),
    re.compile(
        r"(?is)(?:api[_-]?key|access[_-]?token|client[_-]?secret|token|"
        r"password|passwd|secret)[ \t]*\r?\n\s*[:=]\s*(?P<v>\S+)"
    ),
    # A private-key block whose END line was cut off (truncated output) still
    # loses the base64 lines that follow its BEGIN line.
    re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
        r"(?:(?:(?!-----BEGIN ).){0,16384}?"
        r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----"
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
