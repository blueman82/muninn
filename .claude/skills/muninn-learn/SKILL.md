---
name: muninn-learn
description: Control Muninn learning requests and choose future records or verified history with muninn learn.
---

# muninn learn

```sh
muninn learn setup --mode now
muninn learn setup --mode all
muninn learn setup --mode all --confirm SCAN_DIGEST
muninn learn request --session REF [--from-line N --through-line N] [--policy PATH] [--task TASK]
muninn learn list [--state STATE]
muninn learn show R_ID
muninn learn explain R_ID [--human]
muninn learn dismiss R_ID
muninn learn cancel R_ID
muninn learn retry R_ID
muninn learn resume --policy PATH --failed-request R_ID
muninn learn replay --session REF --from-line N --through-line N --policy PATH
```

`now` selects records after the setup boundary.
It excludes indexed history and older or undated late imports.
`all` previews verified project history before confirmation.
Copy the returned `confirmation` value into `SCAN_DIGEST`.
`--project PATH` limits automatic learning to one indexed project.
From-now mode applies to all projects.
The installer offers the same scope choice and retains it during upgrades.

The preview reports eligible requests, excluded source ranges, causes, estimates, and sample confidence.
Capture gaps are not model calls.
Unreported API cost and remaining subscription allowance stay unknown.
Confirmation includes all traversed original bytes and indexed records.
A changed scan refuses confirmation with `history_scan_changed`.
Reindexing changed originals also requires a new confirmation.
Estimates separate first-attempt work from work with permitted retries.
They exclude setup waits and database delays.
An active review refuses scope changes with `learning_busy`.

`REF` identifies an indexed `provider:thread_id` or an unambiguous session root.
Request IDs use `R_` followed by 32 lowercase hexadecimal characters.
Request bounds must be paired.
Without bounds, the request starts after its activation or completion boundary.
A queued request does not prove completed learning.

An installed, admitted policy is required.
An explicit policy file must match that policy.
Missing or invalid policy returns `review_policy_required`.
Never invent runner settings, limits, approval, or evidence.

List returns `requests` containing `RequestView` objects.
Show returns `ReviewDetail` with attempts, calls, evidence, receipt, `correction_status`, and `diagnostics`.
Diagnostics explain the failure stage, cause, and action.

For an explanation in ordinary language, run `muninn learn explain R_ID --human`.
To display only the explanation, redirect JSON stdout:

```sh
muninn learn explain R_ID --human >/dev/null
```

Explain returns `request_id`, `correction_status`, and `explanation` as JSON.
`--human` also prints the final redacted explanation to stderr.
Stdout still contains JSON. `--pretty` still changes only JSON indentation.

The explanation displays no source or proposal text.
Muninn does not retain failed model proposals.
Their exact sentence and cited passage are unavailable.
Evidence counts describe the retained input to the request.
They do not reconstruct a rejected proposal.

Use show to inspect evidence under the existing access checks.
Changed or erased evidence can have no usable excerpt.
Only the commit receipt establishes saved changes.
A rejected batch saves no partial knowledge changes.
Missing model-launch records do not prove that no call occurred.
Saved failure stages identify only the recorded phase.

`correction_status` describes the latest attempt with a durable correction marker, ordered by start time and attempt ID.
`requested` means a marker exists, but its exact next round has no recorded launch.
`attempted` means that next round launched in the same attempt, but its correction result is unproved.
`succeeded` requires a completed next round and a receipt from that same attempt.
`exhausted` requires a completed next round and a terminal failure from that same attempt.
Without a valid marker or enough retained evidence, the status is `unknown`.
Two calls alone do not prove correction: they can be a read and a final result.
A later ordinary success cannot prove that an earlier correction succeeded.
Clearing a failure notice preserves correction markers.
The receipt separately establishes saved changes or `no_change`.

New result failures record one observed phase: `native output`, `model format`, `candidate validation`, `controlled read`, `original evidence`, or `commit`.
Setup and launch checks retain their existing phase labels.
Missing historical phases remain `not recorded`; Muninn does not infer them from codes or call counts.

The explanation separates current admission and setup notices from historical request failures.
Admission controls whether learning can accept work.
A valid stored policy does not prove that setup checks pass or a worker is running.

You can inspect and dismiss a safety rejection.
A rejection code alone does not prove that the wording check was correct.
If Muninn incorrectly rejects direct owner wording, the maintainer must investigate.
For a relevant unresolved question or explicit owner review, use the separate `muninn-know` decision flow.
Ordinary supported learning still saves without a new approval step.
Local operators must restore the approved setup when they repair it.
Maintainers must investigate unclear defects and exhausted source-read allowances.

`evidence_limit` means source verification exceeded a byte, record, or database-work limit.
Do not retry that request unchanged.
Ask the maintainer to inspect its source-read budget.
A narrower window can still require a read of earlier records.
The operation limit includes source traversal, not just selected text.
Capture, catch-up and attempt logs preserve this code without transcript text.

Older capture logs can contain only `unavailable`.
Their exact cause is unknown.
Muninn fails the request without an automatic retry.
Limits and admission stay unchanged.
Older `config` records cannot recover a cause that Muninn did not retain.

Run `muninn doctor --pretty` for current checks.
The old stage cannot recover the cause.

Final diagnostics distinguish these codes:

- `evidence_byte_limit`: physical bytes read.
- `evidence_record_limit`: physical records read.
- `evidence_work_limit`: database work.
- `evidence_selection_limit`: accumulated selected content.

These allowances limit source verification.
They differ from memory storage capacity and model context size.
Muninn does not reconstruct historical configured limits or measured counters.

Request notices include the request ID in show and dismiss commands.
Dismiss returns `request_id` and the boolean `dismissed`.
It removes only that request's saved notice.
It preserves learning state, requests, attempts, calls, evidence, and receipts.
Failed requests remain visible in list and show.

After dismissal, diagnostics use recorded attempt error codes.
The stage is `not recorded`.
The cause and action use their default text.
Inspect the saved stage and action before dismissal if you need them.
A repeated dismissal returns `dismissed: false`.
An unknown request returns `request_unavailable`.

New failures after dismissal can create another notice.
Upgrades preserve saved notices.
At session start, `systemMessage` shows learning actions that require the owner.
This also applies with `recall.off` or `recall.advisory`.
Setup notices take priority over eligible request notices.
Muninn then selects the newest eligible request notice.
A newer contained failure cannot hide an older required action, even when admission is unsafe.
If admission is unsafe and no required action remains, Muninn shows a contained issue with current pause guidance.

With open admission and a valid stored policy and digest, contained request failures stay quiet at startup.
These include rejected proposals, exhausted corrections, bounded retries, and deterministic source-read limits.
The finite contained codes are `deadline`, `unavailable`, `rejected_revision`, `rejected_authority`, `rejected_validation`, `rejected_evidence`, `rejected_erased`, `rejected_scope`, `evidence_limit`, `evidence_byte_limit`, `evidence_record_limit`, `evidence_work_limit`, and `evidence_selection_limit`.
Sign-in, capability, binding, security, native tool events, uncertain processes, approval failures, and unknown codes remain visible.
Setup notices remain visible.
`prompt_limit` remains visible because the maintainer must inspect the prompt budget.
The complete prompt, including any transient untrusted correction baseline, must fit the existing input cap.
Muninn fails before another reservation or launch, without truncation, a limit increase, or an automatic retry.
Do not retry the request unchanged.

Closed admission remains visible even without a saved issue.
An absent, invalid, or mismatched stored policy also produces a startup action.
Muninn reads saved admission and validates the stored policy and digest for this decision.
Saved action wording does not establish current admission.
These checks do not prove live host, account, or worker health.
Startup changes no saved issues, requests, admission, or history.
It displays fixed program text, never question bodies, source text, or model output.
`doctor` still reports `review_attention` for saved issues, including failures hidden at startup.
Inspect contained failures with `muninn learn list --state failed`, `muninn learn show R_ID`, or `muninn learn explain R_ID --human`.
After you dismiss one request notice, an older eligible notice can appear.
Successful completion clears the request notice.

You cannot dismiss a setup notice with a request ID.
Evidence can contain transcript text.
Keep evidence out of logs.
Open cited originals before making factual claims.

Revision 3 allows four workers and three total attempts.
Temporary failures wait 5 seconds, then 30 seconds, before their remaining attempts.
Manual and live requests take priority over queued history.
Within each group, Muninn processes older requests first.
Rejected results do not stop unrelated requests.
The background program can recheck shared setup failures while running and learning is enabled.
Its setup delay is at least 60 seconds. This is not a promised check time.
Reading a saved warning starts no timer.
A failed check retains or replaces the warning. A successful relevant setup check or launch clears it.
Clearing the warning does not establish saved memory.
Current human wording comes from safe codes, never raw saved text.
`codex_host_changed` means the saved computer ID differs. Why it changed remains unknown.
Other distinct setup codes are `codex_approval_required`, `codex_data_home_changed`,
`codex_capability_status`, `codex_platform_changed`, and `codex_project_required`.
An older `codex_experimental_boundary` record did not retain the exact mismatch.
The agent can inspect these refusals. Inspection does not renew approval or enable learning.
Owner pauses remain in force.
Unknown process state blocks learning.
The next Codex session shows required action without a manual command.

Retry explicitly restarts the same request when current checks permit it.
Completed requests keep their receipt.
Resume retains its narrow legacy contract for the latest contained `rejected_authority` failure.
It requires the exact installed policy and completed calls.
It refuses pending work, abandoned attempts, and other failures.
Routine revision 3 recovery does not require resume.
The installer asks separately before enabling paused learning.
`bin/muninn-install --enable-learning` supplies explicit restart approval.
The restart still requires completed calls, ended attempts, and an eligible policy.
It cannot approve a changed custom policy or an uncertain process.

Replay requires explicit bounds and policy.
Never enqueue an archive without authorisation.
Local correction uses `muninn know propose --input FILE`.
A proposal cannot create owner authority.

Output is one JSON object.
`--pretty` changes indentation.
Hooks return compact JSON and exit 0.
Writer exit 3 means busy.
See `docs/REFERENCE.md` for complete shapes and refusal rules.

`--task` binds an explicit request to one task in its indexed project.
Use 1–128 ASCII letters, digits, `.`, `_`, `:`, or `-`.
Start the task ID with a letter or digit.
Without `--task`, the request uses project scope.

End and precompact capture are separate from startup context refresh.
A busy enqueue does not prove durable capture.
Later recovery cannot prove the exact missed event or cutoff.
Cursor waits for native import or explicit full ingest.
Cursor resume and postcompact injection remain unproven.
Use `muninn know context` to request that context explicitly.
Previously injected context cannot be retracted.
Per-prompt recall starts off on fresh installs.

A healthy new project can have no indexed project mapping yet.
Startup then returns normal empty project context. It never uses another project's or global memory instead.
The indexer creates the mapping when it indexes an event.
SessionEnd only requests learning from already indexed eligible records. It does not ingest or wait.
A later startup, resume, clear, or compact can read the new mapping.
A failed startup starts no timer. Prompt recall remains separate and conditional.
`context_unavailable` identifies a context-generation failure whose exact detail was not retained.
Real store, format, privacy, and configuration failures remain visible.
A successful prompt hook does not prove that startup memory reached the conversation.
