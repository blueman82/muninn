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
Show returns `ReviewDetail` with attempts, calls, evidence, receipt, and `diagnostics`.
Diagnostics explain the failure stage, cause, and action.
For ordinary-language inspection, run `muninn learn explain R_ID --human`.
To display only readable prose, redirect JSON stdout: `muninn learn explain R_ID --human >/dev/null`.
Explain returns `request_id` and `explanation` as JSON.
`--human` also prints the final redacted explanation to stderr.
It does not change stdout or `--pretty` semantics.
The explanation displays no source or proposal text.
Failed model proposals are not retained; their exact sentence and cited passage are unavailable.
Evidence counts describe retained input rather than reconstructing a rejected proposal.
Use show for explicit governed evidence inspection; excerpts may be changed or erased.
Only the commit receipt establishes saved impact; a rejected batch saves no partial changes.
Missing model-launch records do not prove that no call occurred.
Saved failure stages identify only the recorded phase.
Current admission and setup notices are distinct from historical request failures.
A valid stored policy does not prove live setup checks pass or a worker is running.
A safety rejection can be valid or can require a classifier repair.
Incorrectly rejected direct owner wording requires maintainer investigation.
No new approval workflow is added.
Operator setup repairs must restore the approved setup.
Unclear defects and exhausted source-read allowances require a Muninn maintainer.
`evidence_limit` means source verification exceeded a byte, record, or database-work limit.
Do not retry that request unchanged; ask the maintainer to inspect its source-read budget.
A narrower window can still require reading earlier records.
The operation limit includes source traversal, not just selected text.
Capture, catch-up and attempt logs preserve this code without transcript text.
Older capture logs may contain only `unavailable`; their exact cause is unknown.
The request fails without automatic retry; limits and admission stay unchanged.
Older `config` records cannot recover the discarded cause.
Run `muninn doctor --pretty` for current checks; do not expect the old stage to recover the cause.
Final diagnostics distinguish `evidence_byte_limit`, `evidence_record_limit`,
`evidence_work_limit`, and `evidence_selection_limit`.
They identify physical bytes, records, database work, or accumulated selected content.
These allowances differ from memory storage capacity and model context size.
Historical configured limits and measured counters are not reconstructed.
Request notices include the request ID in show and dismiss commands.
Dismiss returns `request_id` and the boolean `dismissed`.
It removes only that request's saved notice, without changing learning state.
It retains requests, attempts, calls, evidence, and receipts.
Failed requests remain visible in list and show.
After dismissal, diagnostics use recorded attempt error codes with stage
`not recorded` and the default cause and action.
Inspect the saved stage and action before dismissal if you need them.
A repeated dismissal returns `dismissed: false`.
An unknown request returns `request_unavailable`.
New failures after dismissal can create another notice.
Upgrades preserve saved notices.
Startup selects the newest saved notice, with setup priority.
Dismissing one can reveal an older notice.
Successful completion clears the request notice.
Setup notices take priority and cannot be dismissed with a request ID.
Evidence can contain transcript text.
Keep evidence out of logs.
Open cited originals before making factual claims.

Revision 3 allows four workers and three total attempts.
Temporary failures wait 5 seconds, then 30 seconds, before their remaining attempts.
Manual and live requests take priority over queued history.
Within each group, Muninn processes older requests first.
Rejected results do not stop unrelated requests.
Shared setup failures receive automatic checks every 60 seconds.
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
