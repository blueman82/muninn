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

AGAIN = (
    "Run `bin/muninn-install` again.",
    "If it stops again, run `bin/muninn-install --check` and"
    " `muninn doctor --pretty`.",
    "Send the two outputs to the person who looks after your Muninn"
    " install.",
)

KEEP_RECORDS = "Do not edit or remove the install records."


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


RECORDS = entry(
    "The install found install records that it cannot use.",
    "There is more than one unfinished install, or an install record"
    " changed after Muninn wrote it.",
    KEEP_RECORDS,
    *FACTS,
)

LEGACY = entry(
    "The move from an older Muninn install stopped.",
    "A file, a link, a service, or a setting of the older install changed"
    " after Muninn recorded it. Muninn stops so that it keeps your data.",
    KEEP_RECORDS,
    *FACTS,
)

LEGACY_OPEN = entry(
    "A move from an older Muninn install did not finish.",
    "Muninn must finish or repair that move before it can do a new"
    " install.",
    KEEP_RECORDS,
    *FACTS,
)

RESTART = entry(
    "The install must have a restart of the computer before it can"
    " continue.",
    "Muninn moved the older install. The old service can continue to run"
    " until the computer restarts.",
    "Restart the computer.",
    "Run the same install command again.",
)

PLATFORM = entry(
    "The install cannot do this step on this computer.",
    "This step works only on a different operating system.",
    *FACTS,
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

POLICY_FILE = entry(
    "Muninn did not accept the review policy.",
    "The policy file is not correct, or it has a value that this Muninn"
    " version cannot use.",
    "Examine the policy file that you gave with `--review-policy`.",
    "Correct the file, and run the same install command again.",
    "If it stops again, run `bin/muninn-install --check` and"
    " `muninn doctor --pretty`.",
    "Send the two outputs to the person who looks after your Muninn"
    " install.",
)

POLICY_STATE = entry(
    "Muninn cannot use the review policy that it has now.",
    "The policy in the Muninn data or the default policy of this version"
    " is not correct, or it changed.",
    *FACTS,
)

PREVIEW = entry(
    "Muninn could not read its data to examine the review policy.",
    "The data was busy, it changed, or it had incorrect permissions while"
    " Muninn read it.",
    "Wait until other Muninn commands stop.",
    "Run the same install command again.",
    "If it stops again, run `bin/muninn-install --check` and"
    " `muninn doctor --pretty`.",
    "Send the two outputs to the person who looks after your Muninn"
    " install.",
)

CONSENT = entry(
    "Muninn did not turn on learning.",
    "Muninn must have your answer before it reads your chats for learning."
    " It did not get a yes.",
    "Run the same install command in a terminal.",
    "Read each question, and answer it.",
)

LEARNING = entry(
    "Muninn did not turn on learning again.",
    "The policy that you have now cannot start again with this command, or"
    " a learning task is not complete.",
    *FACTS,
)

HOST = entry(
    "The Codex host approval did not change.",
    "The plan, the approval, or the Codex host changed after you saw the"
    " plan, or a record is not correct.",
    KEEP_RECORDS,
    *FACTS,
)

INTERFACE = entry(
    "The Codex interface change did not continue.",
    "The plan, the record, or the state of Codex is different from the"
    " plan that you approved.",
    KEEP_RECORDS,
    *FACTS,
)

INTERNAL = entry(
    "The install stopped at an internal check.",
    "A step of the install ran without the lock that it must have.",
    *FACTS,
)

FAMILIES: tuple[tuple[Text, tuple[str, ...]], ...] = (
    (
        RECORDS,
        (
            "review_activation_ambiguous",
            "review_activation_record_changed",
            "legacy_record_changed",
            "legacy_record_invalid",
            "legacy_record_missing",
            "legacy_record_unsafe",
            "legacy_records_ambiguous",
        ),
    ),
    (
        LEGACY,
        (
            "legacy_archive_changed",
            "legacy_boot_changed",
            "legacy_candidate_unsafe",
            "legacy_configuration_changed",
            "legacy_configuration_incomplete",
            "legacy_directory_unsafe",
            "legacy_exclusion_changed",
            "legacy_file_changed",
            "legacy_file_unsafe",
            "legacy_installation_changed",
            "legacy_interpreter_changed",
            "legacy_interpreter_mismatch",
            "legacy_key_changed",
            "legacy_knowledge_imported",
            "legacy_launcher_changed",
            "legacy_live_changed",
            "legacy_operation_lock_changed",
            "legacy_owner_lock_changed",
            "legacy_provider_changed",
            "legacy_provider_mismatch",
            "legacy_runtime_changed",
            "legacy_runtime_mismatch",
            "legacy_runtime_returned",
            "legacy_selection_changed",
            "legacy_selection_mismatch",
            "legacy_service_changed",
            "legacy_service_mismatch",
            "legacy_service_returned",
            "legacy_source_ambiguous",
            "legacy_source_changed",
        ),
    ),
    (
        LEGACY_OPEN,
        (
            "legacy_boot_unavailable",
            "legacy_operation_pending",
            "legacy_recovery_required",
        ),
    ),
    (RESTART, ("restart_required",)),
    (
        PLATFORM,
        (
            "legacy_boot_unsupported",
            "legacy_unsupported_platform",
            "host_renewal_unsupported",
            "codex_interface_decision_unsupported",
            "review_policy_unsupported",
        ),
    ),
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
            "cutover_archive_changed",
            "cutover_source_mismatch",
            "maintenance_recovery_required",
            "marked_v1_recovery_required",
            "pause_unverified",
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
    (POLICY_FILE, ("review_policy_invalid", "review_policy_home_changed")),
    (
        POLICY_STATE,
        (
            "default_review_policy_invalid",
            "review_policy_default_changed",
            "review_policy_state_invalid",
            "review_policy_transition_required",
            "learning_custom_policy",
        ),
    ),
    (PREVIEW, ("review_policy_preview_unavailable",)),
    (
        CONSENT,
        (
            "history_not_confirmed",
            "learning_recovery_consent_required",
            "learning_scope_required",
        ),
    ),
    (
        LEARNING,
        (
            "learning_recovery_clock",
            "learning_recovery_policy_required",
            "learning_recovery_unverified",
        ),
    ),
    (
        HOST,
        (
            "host_renewal_approval_required",
            "host_renewal_arguments_invalid",
            "host_renewal_artifacts_changed",
            "host_renewal_containment_required",
            "host_renewal_current_format_required",
            "host_renewal_delta",
            "host_renewal_digest_reused",
            "host_renewal_erasure_invalid",
            "host_renewal_format_required",
            "host_renewal_limit",
            "host_renewal_plan_changed",
            "host_renewal_policy_changed",
            "host_renewal_record_changed",
            "host_renewal_record_invalid",
            "host_renewal_recovery_required",
            "host_renewal_setup_changed",
            "host_renewal_state_changed",
            "host_renewal_state_invalid",
        ),
    ),
    (
        INTERFACE,
        (
            "codex_data_home_changed",
            "codex_host_changed",
            "codex_host_identity_unavailable",
            "codex_interface_approval_required",
            "codex_interface_delta",
            "codex_interface_legacy_approval_invalid",
            "codex_interface_limit",
            "codex_interface_plan_changed",
            "codex_interface_record_changed",
            "codex_interface_record_invalid",
            "codex_interface_recovery_required",
            "codex_interface_runtime_required",
            "codex_interface_state_changed",
            "codex_platform_changed",
            "runtime_interface_unsupported",
            "runtime_probe_unavailable",
            "runtime_selection_unavailable",
            "runtime_selection_unsafe",
            "runtime_structure_limit",
        ),
    ),
    (INTERNAL, ("install_operation_required",)),
)


def table() -> dict[str, Text]:
    """Map each code of the families to its shared text.

    Returns:
        The text of each code.
    """
    return {code: text for text, names in FAMILIES for code in names}
