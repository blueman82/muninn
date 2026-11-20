"""Plain explanations for the codes that the installer prints.

Each explanation comes from a fixed table. The printed detail after the
code can hold a path or an operating-system message, so it never goes
into the explanation (ADR 0004).
"""

from __future__ import annotations

from install.failure_text_codes import (
    FACTS,
    KEEP_RECORDS,
    REPORT,
    Text,
    entry,
    table,
)

LEAVE = (
    "Run `bin/muninn-install --leave-unfinished --check`, and read the plan.",
    "Run `bin/muninn-install --leave-unfinished`, and type `y` to leave the"
    " unfinished install.",
    "Run `bin/muninn-install` again.",
    "If a command stops with `FAILED:`, run `bin/muninn-install --check`"
    " and `muninn doctor --pretty`.",
    "Send the two outputs to the person who looks after your Muninn"
    " install.",
)

NOT_CONTINUED = (
    "An earlier install did not finish, and this install cannot continue"
    " it."
)

UNFINISHED = entry(
    NOT_CONTINUED,
    "An earlier install started to turn on learning, and it stopped. The"
    " Muninn version or a settings file changed after that start.",
    *LEAVE,
)

UNFINISHED_CHANGED = entry(
    NOT_CONTINUED,
    "An earlier install started to turn on learning, and it stopped. Its"
    " release, its policy, or the Muninn data changed after that start.",
    *LEAVE,
)

NOT_LEFT = (
    "Muninn did not leave the unfinished install, and it changed no file."
)

LEAVE_TEXTS: dict[str, Text] = {
    "leave_record_unsupported": entry(
        NOT_LEFT,
        "The command can only leave an unfinished upgrade on macOS or"
        " Linux. This unfinished install is a new install, a move from an"
        " older Muninn install, or a Codex host change. Or there is more"
        " than one unfinished install.",
        KEEP_RECORDS,
        *FACTS,
    ),
    "leave_policy_active": entry(
        NOT_LEFT,
        "The learning policy in the data does not agree with the install"
        " record or with the state before the install, or the install"
        " record is not at its first step, or Muninn cannot read its"
        " data. Or a Codex interface change started to change the"
        " learning rule. An install must finish that change.",
        KEEP_RECORDS,
        *FACTS,
    ),
    "leave_links_changed": entry(
        NOT_LEFT,
        "A Muninn link or the release folder of the unfinished install"
        " changed after that install. Muninn cannot prove that a new install"
        " can use them.",
        KEEP_RECORDS,
        *FACTS,
    ),
    "leave_provider_unreadable": entry(
        NOT_LEFT,
        "Muninn cannot read a settings file of Claude Code, Cursor, or"
        " Codex. A new install must read these files.",
        "Make sure that each of these settings files is correct JSON or"
        " TOML.",
        "Run `bin/muninn-install --leave-unfinished --check` again.",
        *REPORT,
    ),
    "leave_confirmation_required": entry(
        NOT_LEFT,
        "You did not type `y` or `yes`, or the command did not run in a"
        " terminal.",
        "Run `bin/muninn-install --leave-unfinished` in a terminal.",
        "Read the plan, and type `y` to leave the unfinished install.",
    ),
    "leave_record_changed": entry(
        NOT_LEFT,
        "The install record changed while Muninn waited for your answer."
        " Another installer command can cause this.",
        "Make sure that no other `bin/muninn-install` command runs.",
        "Run `bin/muninn-install --leave-unfinished --check`, and read the"
        " plan.",
        "Run `bin/muninn-install --leave-unfinished`, and type `y`.",
    ),
}

DEFAULT = entry(
    "The install stopped. Muninn has no explanation for this code.",
    "This Muninn version has no text for this code.",
    *FACTS,
)

TABLE: dict[str, Text] = {
    **table(),
    "legacy_candidate_changed": UNFINISHED,
    "review_activation_configuration_changed": UNFINISHED,
    "review_activation_runtime_changed": UNFINISHED_CHANGED,
    "review_activation_changed": UNFINISHED_CHANGED,
    "review_activation_store_changed": UNFINISHED_CHANGED,
    "review_policy_snapshot_changed": UNFINISHED_CHANGED,
    "review_policy_snapshot_missing": UNFINISHED_CHANGED,
    **LEAVE_TEXTS,
}


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
