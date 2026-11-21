"""Plain explanations for the codes that the installer prints.

Each explanation comes from a fixed table. The printed detail after the
code can hold a path or an operating-system message, so it never goes
into the explanation (ADR 0004).
"""

from __future__ import annotations

from install.failure_text_codes import FACTS, Text, entry, table

DEFAULT = entry(
    "The install stopped. Muninn has no explanation for this code.",
    "This Muninn version has no text for this code.",
    *FACTS,
)

TABLE: dict[str, Text] = table()


def explain(detail: str) -> list[str]:
    """Give the plain explanation of one printed failure detail.

    Args:
        detail: The text after ``FAILED: ``. Only the part before the
            first colon selects the explanation.

    Returns:
        The explanation lines, without the ``FAILED:`` line.
    """
    key = detail.split(":", 1)[0].strip()
    return list(TABLE.get(key, DEFAULT))
