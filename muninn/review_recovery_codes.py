"""Define finite safe review causes, actions, and classifications."""

from __future__ import annotations

EXPLANATIONS: dict[str, tuple[str, str]] = {
    "deadline": (
        "The review exceeded its time limit.",
        "Muninn will retry within the request limit.",
    ),
    "unavailable": (
        "The review runner did not finish successfully.",
        "Muninn will retry within the request limit.",
    ),
    "config": (
        "Muninn could not verify the review setup. "
        "The specific cause was not recorded.",
        "Run muninn doctor --pretty for current setup checks. "
        "Give unresolved safe diagnostics to the Muninn maintainer.",
    ),
    "prompt_limit": (
        "The complete review prompt exceeded its input byte limit.",
        "Do not retry this request unchanged. "
        "Ask the Muninn maintainer to inspect its prompt budget.",
    ),
    "evidence_limit": (
        "Original-source verification exceeded its byte, record, "
        "or database-work limit.",
        "Do not retry this request unchanged. "
        "Ask the Muninn maintainer to inspect its source-read budget. "
        "A narrower window can still require reading earlier records.",
    ),
    "auth": (
        "Codex sign-in is unavailable.",
        "Open Codex and sign in. Muninn will check again.",
    ),
    "capability_changed": (
        "The installed Codex capability no longer matches its approved setup.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "capability_unverified": (
        "The runner capability has no current verification.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "capability_config": (
        "The runner configuration differs from its approved settings.",
        "Restore the approved settings. Muninn will check again.",
    ),
    "effective_host_changed": (
        "The Codex setup differs from its approved settings.",
        "Restore the approved Codex settings. Muninn will check again.",
    ),
    "launch_input_unavailable": (
        "A required review capability or prompt file is unavailable.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "launch_input_binding": (
        "A review capability or prompt does not match the installed policy.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "launch_input_invalid": (
        "A required review setup file is invalid.",
        "Restore the approved setup file. Muninn will check again.",
    ),
    "rejected_authority": (
        "The proposed change lacks verified owner evidence.",
        "Muninn rejected this result and will continue with other requests.",
    ),
    "rejected_revision": (
        "Saved knowledge changed while this review ran.",
        "Muninn will rebuild the review within the request limit.",
    ),
    "rejected_validation": (
        "The model result did not match the required format.",
        "Muninn rejected this result and will continue with other requests.",
    ),
    "rejected_evidence": (
        "Source evidence is missing, changed, or unverifiable.",
        "Muninn excluded this request. Verified requests can continue.",
    ),
    "rejected_erased": (
        "The request refers to erased evidence.",
        "Muninn excluded this request. Verified requests can continue.",
    ),
    "rejected_scope": (
        "The proposed change is outside the admitted project scope.",
        "Muninn rejected this result and will continue with other requests.",
    ),
    "native_tool_event": (
        "Codex reported a tool action during a review.",
        "Inspect the Codex activity before enabling learning again.",
    ),
    "process_identity_unverified": (
        "Muninn cannot verify whether an earlier review process stopped.",
        "Keep learning paused until Muninn can verify the process.",
    ),
}
EXPLANATIONS.update(
    {
        f"evidence_{kind}_limit": (
            f"Original-source verification exceeded its {allowance} limit.",
            EXPLANATIONS["evidence_limit"][1],
        )
        for kind, allowance in (
            ("byte", "physical byte-read"),
            ("record", "physical record-read"),
            ("work", "database-work"),
            ("selection", "accumulated selected-content"),
        )
    }
)
_SETUP_FAILURES = {
    "capability_unavailable": (
        "Muninn cannot read a required runner executable, "
        "proof, or schema file."
    ),
    "capability_invalid": (
        "A runner capability or proof file has an invalid format."
    ),
    "capability_path": (
        "A runner path is relative or resolves to a different path."
    ),
    "capability_storage": (
        "A runner setup file or directory fails "
        "its ownership or access checks."
    ),
    "capability_executable_storage": (
        "The runner executable fails its ownership or access checks."
    ),
    "capability_executable_provenance": (
        "Muninn cannot verify the runner executable's origin."
    ),
    "capability_platform": (
        "The runner is unavailable on this operating system or processor."
    ),
    "capability_limit": (
        "The runner setup exceeds a file size or scan limit."
    ),
    "codex_experimental_boundary": (
        "The saved Codex setup does not match. The exact mismatch "
        "was not recorded."
    ),
    "codex_approval_required": "The saved Codex approval is missing.",
    "codex_data_home_changed": (
        "The saved data directory does not match this data " "directory."
    ),
    "codex_capability_status": (
        "The runner status does not match the approved setup."
    ),
    "codex_platform_changed": (
        "This operating system or processor does not match the "
        "approved setup."
    ),
    "codex_host_changed": (
        "The saved computer ID does not match this computer. Why it"
        " changed is unknown."
    ),
    "codex_project_required": "This review has no project binding.",
    "native_prompt_instruction": (
        "The review instructions do not match the required Codex format."
    ),
    "launch_input_platform": (
        "This operating system cannot use the approved review setup."
    ),
    "launch_input_storage": (
        "A review setup file is outside its required private directory."
    ),
    "runner_unsupported": (
        "The selected runner has no supported launch method."
    ),
}
EXPLANATIONS.update(
    {
        code: (
            cause,
            "The agent can inspect the saved approval. "
            "The background program checks again only while running and"
            " learning is enabled. "
            "If the check still fails, the review stays blocked. "
            "A successful setup check clears this notice. Reading it "
            "starts no timer.",
        )
        for code, cause in _SETUP_FAILURES.items()
    }
)
EXPLANATIONS["codex_host_changed"] = (
    _SETUP_FAILURES["codex_host_changed"],
    "The agent checks current format 2 and recovery prerequisites, then "
    "prepares a host-only candidate and a non-writing renewal preview: "
    "bin/muninn-install --upgrade --review-policy PATH "
    "--renew-codex-host preview --check. "
    "The owner must approve that exact plan at the installer prompt. "
    "A preview digest or reading this notice grants no permission. "
    "Learning stays paused if it was paused. Reading starts no timer.",
)
SHARED = frozenset(
    {
        "auth",
        "config",
        "effective_host_changed",
        "capability_changed",
        "capability_unverified",
        "capability_config",
        "launch_input_unavailable",
        "launch_input_binding",
        "launch_input_invalid",
    }
).union(_SETUP_FAILURES)

for _code in SHARED:
    _cause, _action = EXPLANATIONS[_code]
    EXPLANATIONS[_code] = (
        _cause,
        _action.replace(
            "Muninn will check again.",
            "The background program can check again only while running "
            "and learning is enabled. Reading this notice starts no timer.",
        ),
    )

CONTAINED = frozenset(
    {
        "deadline",
        "unavailable",
        "rejected_revision",
        "rejected_authority",
        "rejected_validation",
        "rejected_evidence",
        "rejected_erased",
        "rejected_scope",
        "evidence_limit",
        "evidence_byte_limit",
        "evidence_record_limit",
        "evidence_work_limit",
        "evidence_selection_limit",
    }
)
STARTUP_STAGES = frozenset(
    {
        "native output",
        "model format",
        "candidate validation",
        "controlled read",
        "original evidence",
        "commit",
        "evidence",
        "prompt",
        "launch",
        "review",
        "runner",
        "recovery",
        "runner setup file",
        "review instructions",
    }
)
