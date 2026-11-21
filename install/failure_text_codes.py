"""Fixed plain texts for families of installer codes.

Codes with the same cause and the same steps share one text. Each code
still has its own key in the table.
"""

from __future__ import annotations

Text = tuple[str, ...]

FACTS = (
    "Run `bin/muninn-install --check`, and copy the output.",
    "Run `muninn doctor --pretty`, and copy the output.",
    "Send the two outputs to the person who looks after your Muninn"
    " install.",
)

REPORT = (
    "If it stops again, run `bin/muninn-install --check` and"
    " `muninn doctor --pretty`.",
    "Send the two outputs to the person who looks after your Muninn"
    " install.",
)

AGAIN = ("Run `bin/muninn-install` again.", *REPORT)


def entry(happened: str, why: str, *steps: str) -> Text:
    """Build one explanation in the fixed plain form.

    Args:
        happened: What the person sees has happened.
        why: The cause.
        *steps: The numbered steps, in order.

    Returns:
        The explanation lines.
    """
    numbered = tuple(f"{n}. {step}" for n, step in enumerate(steps, 1))
    return (
        f"What happened: {happened}",
        f"Why: {why}",
        "What to do:",
        *numbered,
    )


LOCK = entry(
    "The install could not use its lock file.",
    "Another installer command ran at the same time, or the lock file or"
    " the release folder has incorrect permissions.",
    "Make sure that no other `bin/muninn-install` command runs.",
    *AGAIN,
)

RUNTIME = entry(
    "The install could not prepare Python for the new Muninn version.",
    "Muninn could not make or examine the Python packages of the new"
    " version.",
    "Make sure that Python 3.13 is installed.",
    "Run `bin/muninn-install --check`, and read the output.",
    *AGAIN,
)

STORE = entry(
    "The install stopped to keep your Muninn data safe.",
    "The Muninn data or its copy is not in the state that the install"
    " expects.",
    "Do not edit or remove files in the Muninn data folder.",
    *FACTS,
)

FRESH = entry(
    "The new install stopped.",
    "The Muninn data folder already exists, it changed during the install,"
    " or it has incorrect permissions.",
    "Do not remove the Muninn data folder.",
    *FACTS,
)

FAMILIES: tuple[tuple[Text, tuple[str, ...]], ...] = (
    (
        LOCK,
        ("install_lock_changed", "install_lock_unsafe", "install_root_unsafe"),
    ),
    (
        RUNTIME,
        (
            "runtime_bootstrap_failed",
            "runtime_bootstrap_unavailable",
            "runtime_lock_missing",
            "runtime_provision_failed",
            "runtime_provision_unavailable",
        ),
    ),
    (
        STORE,
        (
            "archive_restore_requires_review",
            "current_store_invalid",
            "cutover_source_mismatch",
        ),
    ),
    (
        FRESH,
        (
            "fresh_home_changed",
            "fresh_home_exists",
            "fresh_home_not_private",
            "fresh_ingest_incomplete",
        ),
    ),
)


def table() -> dict[str, Text]:
    """Map each code of the families to its shared text.

    Returns:
        The text of each code.
    """
    return {code: text for text, names in FAMILIES for code in names}
