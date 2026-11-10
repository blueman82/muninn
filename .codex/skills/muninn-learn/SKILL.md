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
It does not include indexed history.
It also does not include older imports and imports with no date that arrive subsequently.
`all` previews checked project history before confirmation.
Copy the reported `confirmation` value into `SCAN_DIGEST`.

`--project PATH` limits automatic learning to one indexed project.
From-now mode applies to all projects.
The installer offers the same scope selection and keeps it during upgrades.
An ordinary upgrade with no new learning or policy `request` keeps the recorded scope and its `original` boundary.

It specifies no `review` of more history and queues none of that history to complete the upgrade.
An explicit scope or policy `request` uses the usual `review` and consent checks.

The preview reports eligible `requests`, excluded source ranges, causes, estimates, and sample confidence.
Capture `gaps` are not model `calls`.
Unreported API cost and remaining subscription allowance stay unknown.
Confirmation includes all traversed `original` bytes and indexed records.

A changed scan refuses confirmation with `history_scan_changed`.
Reindexing changed `originals` also makes a new confirmation necessary.
Estimates distinguish first-attempt work from work with permitted retries.
They do not include setup or database waits.

An active `review` refuses scope changes with `learning_busy`.

`REF` identifies an indexed `provider:thread_id` or an unambiguous session root.
`Request` IDs use `R_` followed by 32 lowercase hexadecimal characters.
`Request` `bounds` must be paired.
Without `bounds`, the `request` starts after its activation or end boundary.

A queued `request` does not prove completed learning.

An installed, admitted policy is necessary.
An explicit policy file must be the same as that policy.
Missing or invalid policy reports `review_policy_required`.
Do not invent runner settings, limits, approval, or `evidence`.

List reports `requests` containing `RequestView` objects.
Show reports `ReviewDetail` with `attempts`, `calls`, `evidence`, receipt, `correction_status`, and `diagnostics`.
Diagnostics tell the error `stage`, cause, and `action`.

For an explanation in ordinary language, run `muninn learn explain R_ID --human`.
To display only the explanation, redirect JSON stdout:

```sh
muninn learn explain R_ID --human >/dev/null
```

`explain` reports `request_id`, `correction_status`, and `explanation` as JSON.
`--human` also prints the last redacted explanation to stderr.
Stdout contains JSON.
`--pretty` changes only JSON indentation.

The explanation displays no source or proposal text.
Muninn does not keep unsuccessful model proposals.
Their unchanged sentence and cited excerpt are unavailable.
`Evidence` counts report the kept input to the `request`.

They do not reconstruct a rejected proposal.

Use show to examine `evidence` with the current access checks.
Changed or erased `evidence` can have no usable excerpt.
Only the commit receipt proves recorded changes.
A rejected batch records no knowledge changes from that batch.

Missing model-launch records do not prove that no `call` occurred.
Recorded error phases identify only the recorded phase.

`correction_status` reports the latest `attempt` with a durable correction marker, in start-time and `attempt`-ID sequence.
`requested` means a marker exists, but its specified next `round` has no recorded launch.
`attempted` means that next `round` launched in the same `attempt`, but its correction result is unproved.
`succeeded` needs a completed next `round` and a receipt from that same `attempt`.

`exhausted` needs a completed next `round` and a terminal error from that same `attempt`.
Without a correct marker or sufficient kept `evidence`, the status is `unknown`.
Two `calls` alone do not prove correction: they can be a read and a `final` result.
A subsequent ordinary success cannot prove that an earlier correction succeeded.

Clearing an error notice keeps correction markers.
The receipt independently proves recorded changes or `no_change`.

New result errors record one measured phase: `native output`, `model format`, `candidate validation`, `controlled read`, `original evidence`, or `commit`.
Setup and launch checks keep their current phase labels.
Missing historical phases stay `not recorded`.
Muninn does not infer them from codes or `call` counts.

The explanation distinguishes current admission and setup notices from historical `request` errors.
Admission controls if learning can accept work.
A correct stored policy does not prove that setup checks pass or a worker runs.

You can examine and dismiss a safety refusal.
A refusal code alone does not prove that the wording check was correct.
If Muninn incorrectly rejects direct owner wording, the maintainer must examine the cause.
For a related unresolved question or explicit owner `review`, use the different `muninn-know` decision flow.

Ordinary learning with checked `support` records without a new approval step.
Local operators must put back the approved setup when they repair it.
Maintainers must examine unclear defects and exhausted source-read allowances.

`evidence_limit` means source verification exceeded a byte, record, or database-work limit.
Do not retry that `request` unchanged.
Tell the maintainer to examine its source-read budget.
A narrower window can make a read of earlier records necessary.

The operation limit includes source traversal, not only selected text.
Capture, catch-up and `attempt` logs keep this code without transcript text.

Older capture logs can contain only `unavailable`.
Their unchanged cause is unknown.
Muninn fails the `request` without an automatic retry.
Limits and admission stay unchanged.

Older `config` records cannot recover a cause that Muninn did not keep.

Run `muninn doctor --pretty` for current checks.
The previous `stage` cannot recover the cause.

Last diagnostics distinguish these codes:

- `evidence_byte_limit`: physical bytes read.

- `evidence_record_limit`: physical records read.

- `evidence_work_limit`: database work.

- `evidence_selection_limit`: accumulated selected content.

These allowances limit source verification.
They are different from memory storage capacity and model context capacity.
Muninn does not reconstruct historical configured limits or measured counters.

`Request` notices include the `request` ID in show and dismiss commands.
Dismiss reports `request_id` and the boolean `dismissed`.
It removes only that request's recorded notice.
It keeps learning `state`, `requests`, `attempts`, `calls`, `evidence`, and receipts.

Unsuccessful `requests` stay readable in list and show.

After dismissal, diagnostics use recorded `attempt` error codes.
The `stage` is `not recorded`.
The cause and `action` use their default text.
Examine the recorded `stage` and `action` before dismissal if they are necessary.

A duplicate dismissal reports `dismissed: false`.
An unknown `request` reports `request_unavailable`.

New errors after dismissal can make a new notice.
Upgrades keep recorded notices.
At session start, `systemMessage` shows learning steps that make owner input necessary.
This also applies with `recall.off` or `recall.advisory`.

Muninn shows setup notices before eligible `request` notices.
Muninn then selects the newest eligible `request` notice.
A newer contained error cannot hide an older necessary `action`, when admission cannot safely continue.
If admission cannot safely continue and no necessary step stays, Muninn shows a contained issue with current pause guidance.

With open admission and a correct stored policy and digest, contained `request` errors stay quiet at startup.
These include rejected proposals, exhausted corrections, bounded retries, and deterministic source-read limits.
The finite contained codes are `deadline`, `unavailable`, `rejected_revision`, `rejected_authority`, `rejected_validation`, `rejected_evidence`, `rejected_erased`, and `rejected_scope`.
They also include `evidence_limit`, `evidence_byte_limit`, `evidence_record_limit`, `evidence_work_limit`, and `evidence_selection_limit`.
Sign-in, `capability`, binding, `security`, native tool `events`, uncertain `processes`, approval errors, and unknown codes stay readable.

Setup notices stay readable.
`prompt_limit` stays readable because the maintainer must examine the prompt budget.
The complete prompt, with transient untrusted correction baselines, must fit the current input cap.
Muninn fails before one more reservation or launch, without truncation, a limit increase, or an automatic retry.

Do not retry the `request` unchanged.

Closed admission stays readable without a recorded issue.
A missing, invalid, or mismatched stored policy also makes a startup `action`.
Muninn reads recorded admission and validates the stored policy and digest for this decision.
Recorded `action` wording does not prove current admission.

These checks do not prove live host, account, or worker health.
Startup changes no recorded issues, `requests`, admission, or history.
It displays constant software text, no question bodies, source text, or model output.
`doctor` reports `review_attention` for recorded issues, with errors hidden at startup.

Examine contained errors with `muninn learn list --state failed`, `muninn learn show R_ID`, or `muninn learn explain R_ID --human`.
After you dismiss one `request` notice, an older eligible notice can show.
End without an error clears the `request` notice.

You cannot dismiss a setup notice with a `request` ID.
`Evidence` can contain transcript text.
Keep `evidence` out of logs.
Open cited `originals` before making factual claims.

Revision 3 allows four workers and three total `attempts`.
Temporary errors wait 5 seconds, then 30 seconds, before their remaining `attempts`.
Muninn works on manual and live `requests` before queued history.
In each group, Muninn handles older `requests` first.

Rejected results do not stop unrelated `requests`.
The background software can check shared setup errors again while it runs and learning is on.
Its setup wait is at minimum 60 seconds.
This is not a promised check time.

A warning starts no timer when you read it.
An unsuccessful check keeps or replaces the warning.
A correct related setup check or launch clears it.
Clearing the warning does not prove recorded memory.

Current human wording comes from safe codes.
It does not use raw recorded text.
`codex_host_changed` means the recorded computer ID differs.
Why it changed stays unknown.
Other different setup codes are `codex_approval_required`, `codex_data_home_changed`, `codex_capability_status`, `codex_platform_changed`, and `codex_project_required`.

An older `codex_experimental_boundary` record did not keep the specified mismatch.
The agent can examine these refusals.
These checks give no new approval and do not start learning.
Owner pauses stay in force.

Unknown `process` `state` blocks learning.
The next Codex session shows necessary `action` without a manual command.

Retry explicitly restarts the same `request` when current checks permit it.
Completed `requests` keep their receipt.
`Resume` keeps its narrow legacy contract for the latest contained `rejected_authority` error.
The unchanged installed policy and completed `calls` are necessary.

It refuses pending work, abandoned `attempts`, and other errors.
Usual revision 3 recovery does not make `resume` necessary.
The installer asks independently before enabling paused learning.
`bin/muninn-install --enable-learning` supplies explicit restart approval.

The restart makes completed `calls`, ended `attempts`, and an eligible policy necessary.
It cannot approve a changed custom policy or an uncertain `process`.

Replay needs explicit `bounds` and policy.
Do not enqueue an archive without authorization.
Local correction uses `muninn know propose --input FILE`.
A proposal cannot make owner authority.

Output is one JSON object.
`--pretty` changes indentation.
Hooks report compact JSON and exit 0.
Writer exit 3 means busy.

See `docs/REFERENCE.md` for complete shapes and refusal rules.

`--task` binds an explicit `request` to one task in its indexed project.
Use 1–128 ASCII letters, digits, `.`, `_`, `:`, or `-`.
Start the task ID with a letter or digit.
Without `--task`, the `request` uses project scope.

End and precompact capture are different from startup context refresh.
A busy enqueue does not prove durable capture.
Subsequent recovery cannot prove which `event` or cutoff was missed.
Cursor waits for native import or explicit full index.

Cursor context on resume or after compaction stays unproven.
Use `muninn know context` to get that context explicitly.
Previously injected context cannot be retracted.
Per-prompt recall starts off on new installs.

A healthy new project can have no indexed project mapping.
Startup then reports usual empty project context.
It does not use a different project's or global memory as a replacement.
The indexer makes the mapping when it indexes an `event`.

SessionEnd only makes learning `requests` for currently indexed eligible records.
It does not index or wait.
A subsequent startup, `resume`, clear, or compact can read the new mapping.
An unsuccessful startup starts no timer.

Prompt recall stays different and conditional.
`context_unavailable` identifies a context-generation error with no recorded specified information.
Actual store, format, privacy, and configuration errors stay readable.
A prompt hook that completes without an error does not prove that startup memory reached the conversation.


For a removed previous Codex executable on Darwin/arm64, prepare the managed interface rule with `bin/muninn-install --upgrade --codex-interface preview --check`.
This writes nothing and runs only fixed isolated model-free checks.
Apply only its same digest with `--codex-interface PLAN_DIGEST`.
The installer needs new yes/y consent to that initial UPDATE RULE.

A preview or source `review` is not activation approval.

This one approval also enrolls the physical computer.
The previous name-based digest does not prove physical continuity.
Preview reports that continuity as unverified.
Keep its observed_legacy_host value as a preview diagnostic only.

A subsequent computer-name change on the same physical target does not make new approval necessary.

The new target has `kind` darwin-ioplatformuuid-v1 and a digest.
Muninn reads the public Darwin ID one time, with a positive native wait of at maximum 50 ms.
It keeps no raw ID or name.
The wait does not limit all operating-system wait.

A different physical ID stops with codex_host_changed.
An unavailable checked ID stops with codex_host_identity_unavailable.
Do not use a different ID as a replacement.

The selected runtime must declare codex-managed-host-enrollment-v1 and parse ongoing-mac-codex-managed-v1 terms before grant commit or restart.
A previous selection marker cannot pass.
A runtime downgrade after commit keeps the new grant stopped for checked recovery.
Run again the same operation with compatible code.

Do not put back the previous grant or start the service by hand.
Ordinary upgrades do not enroll it.
Do not use it with host renewal, `--enable-learning`, a new install, new scope/history or a different policy input.

The rule follows only `~/.codex/packages/standalone/current` in `releases`.
Compatible updates keep the same policy, account, auth home, model/`reasoning`, permissions, scope, history, `attempts`, charges and limits.
They make no version question necessary.
Local manager trust does not prove each helper's publisher `origin`.

The rule has no arbitrary PATH/project selection, copied previous runtime, or new billing method.

Managed `attempts` expose stable selection.
Managed `calls` expose a versioned `codex-managed-call-v1` `requested` record with up to two candidates, offers/refusals, refresh count and write-once actual `launched_identity`.
An offer is not a launch.
Native measured facts stay unknown when unreported.

Each `round` keeps its own facts.
One proved pre-exec race can refresh the same reservation.
A second race stops with `runtime_race_exhausted`.
It makes no more `attempt` or refunded charge.

Started or uncertain execution does not authorize automatic relaunch.

Different setup codes are `runtime_selection_unavailable`, `runtime_selection_unsafe`, `runtime_interface_unsupported`, `runtime_probe_unavailable` and `runtime_structure_limit`.
A missing or safe invalid disposable cache needs the same isolated checks again.
Cache storage that fails privacy or identity checks refuses.
Preview does not repair it.

A missing approval or initial bridge cannot reconstruct consent.
Managed estimates report unknown runtime without a checked current identity.

Keep the private typed installer receipt after an unsuccessful activation operation.
Checked recovery recognizes the same committed successor and keeps a subsequent pause.
Do not put back an archive or run previous code against managed JSON.
No DDL changed.

Source tests do not prove installation, activation, native/model or billing `acceptance`.
An ordinary `--enable-learning` restart after managed enrollment checks and uses again the same grant.
Keep its scope, queue, approval time, account and limits.
Unfinished or uncertain `calls` refuse restart.
