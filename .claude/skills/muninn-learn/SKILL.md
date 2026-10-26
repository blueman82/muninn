---
name: muninn-learn
description: Control Muninn learning requests and choose future records or verified history with muninn learn.
---

# muninn learn

```sh
muninn learn setup --mode now
muninn learn setup --mode all
muninn learn setup --mode all --confirm SCAN_DIGEST
muninn learn request --session REF [--from-line N --through-line N] [--policy PATH]
muninn learn list [--state STATE]
muninn learn show R_ID
muninn learn cancel R_ID
muninn learn retry R_ID
muninn learn resume --policy PATH --failed-request R_ID
muninn learn replay --session REF --from-line N --through-line N --policy PATH
```

`now` selects records after the setup boundary.
It excludes indexed history and older or undated late imports.
`all` previews verified project history before confirmation.
Copy the returned `confirmation` value into `SCAN_DIGEST`.
`--project PATH` can narrow history to one indexed project.
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
Evidence can contain transcript text.
Keep evidence out of logs.
Open cited originals before making factual claims.

Revision 3 allows four workers and three total attempts.
Temporary failures wait 5 seconds, then 30 seconds, before their remaining attempts.
Live requests take priority over queued history.
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
