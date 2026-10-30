# 0017: Choose learning scope and recover bounded failures

## Status

Accepted by the owner on 26 October 2026.
This decision updates the default limits and refresh rules in ADR 0015.

## What this decision does

Muninn reads conversation records and proposes useful facts or instructions to keep.
It checks each proposal before it saves knowledge.
One failed proposal must not stop other safe work.
A small, safe correction can use a call that the job already permits.

This decision also lets the owner choose which conversations Muninn can review.
Ordinary safe learning does not require approval for each memory.
[ADR 0018](0018-conversational-memory-decisions.md) covers the separate cases that need an owner decision.

## Terms

- **Proposal:** A change that Muninn has not yet accepted as saved knowledge.
- **Evidence:** An original conversation record that supports a statement.
- **Job:** A request for Muninn to review specified evidence within fixed limits.
  The software calls it a request.
- **Attempt:** One try to complete a job.
  A temporary failure can permit another attempt.
- **Round:** One model call within an attempt.
  A round can request an allowed evidence read or return a final proposal.
- **Worker:** A process that runs one job.
- **Review range:** The choice of which conversations Muninn can review.
  From now and All available history are review ranges.
- **Knowledge scope:** The global, project, and optional task limits that control where knowledge can apply.
  Later references to scope mean knowledge scope unless they explicitly say review range.
- **Revision:** A numbered version of a saved record.
- **Transaction:** A database operation that saves all its changes together or saves none.
- **Generation check:** Muninn saves a current work version number, called a generation.
  Each review attempt keeps the generation number it started with.
  When the current number changes, older attempts cannot save.
  The generation check compares the attempt's number with the current number.
  The software also calls this a generation fence.
- **Schema fingerprint:** A check of the database table structure against a known supported structure.

## Context

Previously, one failed request stopped Muninn from accepting new ongoing Codex learning jobs.
A new user could not select older records for review during installation.
A single worker also delayed large history jobs.

## Decision

### Choose and preserve the review range

Offer From now and All available history during first setup.
Keep the selected review range across upgrades and rebuilds.
Before confirmation, verify the history.
Show excluded records before confirmation.
Bind confirmation to every original byte and indexed record that the scan reads.
After Muninn indexes changed originals again, invalidate the earlier confirmation.

Use existing request identities to prevent duplicate jobs.
Exclude sources that Muninn cannot verify.

### Limit calls and concurrent work

Use policy revision 3 as the new default.
Permit four workers and three total attempts per request.
Give manual and live work priority over queued history.
Keep creation order within each priority group.

Keep SQLite writes on the main thread.
Inside the save transaction, check current knowledge metadata.
If results become stale, prepare fresh results within the same attempt limit.

For safe temporary failures, wait 5 seconds and then 30 seconds before retries.
Reject a result with a fixed invalid condition once.
After a request fails, continue unrelated safe requests.
Recheck unavailable setup every 60 seconds.
Keep owner pauses and unresolved process stops in force.
Preserve all existing evidence, authority, scope, erasure, and runner security checks.
Authority checks verify who can authorize a change.

### Preserve storage and recovery protection

Add `learning_setup` and `review_issue` as optional state tables.
Keep the existing marked store version and required table structures for this unit.
ADR 0018 separately governs the later database-format change.
For copy and rebuild operations, retain both supported schema fingerprints.
Copy the optional tables when they exist.

The installer creates state while maintenance pauses normal work.
A store snapshot protects that change.
Older maintenance tools can refuse the extended schema.
Rollback uses the retained store snapshot.

### Refresh eligible defaults

Refresh unchanged repository defaults from revision 1 or 2 to revision 3.
Preserve old request policies and saved knowledge.
Mark unstarted requests under old policies as replaced.
Use the selected review range to create fresh requests.

Preserve custom policies that someone changed.
Keep owner pauses unless the person separately authorizes a checked restart.
Use the recorded runner version to identify unchanged legacy defaults.

Before restart, require every recorded call and attempt to have finished.
If a process might still run, keep it blocked.
Confirm replacement requests.
Do not move an existing From now start time.

At the next Codex session start, show required action.
Provide safe diagnostic details through `learn show` and `doctor`.
Keep local HTML and video previews ignored by Git.

## Consequences

Four workers can reduce elapsed time.
They can also consume the provider allowance faster.
Concurrent knowledge changes can require fresh calls.
Three attempts limit retries for one request.
They do not limit account cost or subscription allowance.

Estimate whole worker batches from comparable recorded calls.
Show first-attempt estimates separately from estimates with permitted retries and delays.
Exclude setup waits and database delays from these estimates.

Source verification can exclude history that exceeds the original-read limit.
The scan reports these exclusions and their causes.
Muninn cannot restore missing originals or authorize changed security settings.
A person must provide missing account access or approval when required.

Use measured queue age, retries, and revision conflicts to reconsider worker count and history interval size.

## Proposed amendment: bounded correction and action routing

Status: ACCEPTED by the owner on 2026-10-29.
The unit 1 amendment is implemented in source.
Unit 1 was merged in PR #77 at `5fa95b51e5628ca52082da846d2746473cbc961b`.
Installation and live verification remain separate delivery checks.
The conversational decisions in ADR 0018 form a separate unit.
That unit is implemented in the private source tree.
Fresh review, its final full gate, publication, installation, and live verification remain pending.
Owner authorization: “all above is approved what questions would you ask”.

The accepted decision above remains in force except for this narrow correction and presentation amendment.
This approval records authority to change the project.
It does not prove an owner's decision about any runtime memory proposal.

See the [memory lifecycle design](../memory-lifecycle-design.md).
The separate [ADR 0018 proposal](0018-conversational-memory-decisions.md) covers owner decisions and their database records.

### Correction within the existing allowance

Permit at most one correction call when an existing review round remains.
Keep these limits unchanged:

- Two rounds by default, three total attempts, and four workers.
- Deadlines and permit accounting.
- The 5-second and 30-second delays for temporary failures.

A preceding controlled read can consume the remaining round.
A controlled read lets the model request only evidence that the job permits.
The correction must return a final operation.
It cannot request another read.

The model runner is the program that starts the model and returns its response.
The native decoder reads that response format.
The JSON decoder reads the structured data inside the response.
The schema validator checks the permitted operation and its required data fields.

The supervisor is Muninn's program that controls the review job.
A correction child is a new model process that the supervisor starts for a correction.

Allow only these two correction forms:

1. **Format correction:** The native decoder and strict JSON decoder accept the output.
   The local union or schema validator then rejects its structure.
   These validators check the permitted operation and required data fields.
   An unknown operation can qualify at this boundary.
   Before validation succeeds, do not trust the field that selects the operation.
2. **Subset reduction:** A fully typed final result fails one of the candidate checks listed below.
   A fully typed result has passed schema validation.
   A candidate is one proposed change or unresolved item in that result.

   Remove at least one candidate.
   Keep every remaining candidate in its original order and exact normalized form.
   Normalization means the required preparation of data before comparison or storage.
   Apply this rule to changes and unresolved items.
   For an empty result, use the fixed no-change outcome.

Permit reduction only for these candidate failures:

- Scope mismatch.
- Missing direct-owner support.
- A repeated replacement target.
- Overlap between replacement targets and affected unresolved targets.

A target is the saved record that a proposal would replace or affect.
Direct-owner support means verified original evidence from the owner.
An unresolved item records a question that Muninn cannot safely settle.

A scope mismatch permits removal only.
Missing owner support also permits removal only.
It cannot authorize a different kind of knowledge or new support.
All other authority failures end that result.
During reduction, do not add or rewrite any of these fields:

- Text, kind, action, scope, target, or revision.
- Supports, dependencies, expiry, claims, or unresolved items.

Never correct these failures:

- Native framing or JSON failures.
- Prohibited native tool events, runner failures, or failures to contain a process.
- Input/output limits or invalid security bindings.

A security binding connects a call to its approved host, policy, and launch input.
These failures end that result without correction:

- Controlled-read refusals and source-read limits.
- Changed originals, erased or unavailable evidence, and unknown evidence references.
- Stale or invalid targets, policy changes, and failed generation checks.

### Give fixed feedback and retain no rejected draft

Do not classify safety failures from exception text.
For format correction, use fixed feedback that the program controls.
For reduction, use a typed permitted failure with the candidate's position.
Feedback contains only fixed codes and candidate coordinates.
It contains no rejected text or raw exception messages.

Give the fresh correction child fixed instructions that permit a final result only.
For reduction, give removal-only rules.
Give this exact empty-final form:

```json
{"op":"final","result":{"schema_version":1,"changes":[],"unresolved":[],"no_change_reason":"No supported candidates remain after correction."}}
```

For reduction, send the fully typed normalized original batch separately as untrusted temporary prompt data.
The software calls this original batch the canonical baseline.
Its content cannot become trusted instructions, evidence, or owner authority.
Do not retain raw output with an invalid schema.

Only the supervisor may defer a permitted candidate failure for correction.
First require successful original-evidence checks and a remaining correction round.
Roll back the rejected transaction without governed writes or final attempt records.
Governed writes are changes subject to Muninn's validation, maintenance, and erasure rules.
Other callers retain existing behavior.

Keep the original normalized batch in temporary round state for independent exact comparison.
Send its normalized representation only to the next correction child as untrusted data.
Do not save this batch in history, call markers, diagnostics, logs, or receipts.
Only independently accepted knowledge follows the normal retention rules.
After recovery, block the old attempt instead of reconstructing a lost batch.

### Recheck safety before correction and save

Before correction, refresh the active request and the complete set of evidence it permits.
Recheck policy, generation, rounds, original-read budgets, deadline, and knowledge freshness.
At the corrected save, repeat all current checks.
Use no extra preflight evidence read.
Do not acquire the database write lock again while the outer operation holds it.
Do not save rejected model content as diagnostic data.

Count the complete UTF-8 prompt against the existing `max_input_bytes` cap.
Include fixed instructions, feedback, and the original batch.
If the prompt cannot fit, fail `prompt_limit` before another reservation or launch.
Do not truncate the original batch or omit required context.
Do not increase limits or add calls.

### Startup action and complete inspection

Select startup actions from fixed failure codes.
Keep ordinary safely contained request failures in doctor, request history, and explanations.
Do not require dismissal merely to stop repeated SessionStart notices.
Preserve the existing `notice()` selection.
Preserve every issue that doctor reports as requiring attention.

Startup still shows these failures or required actions:

- Account access, unverifiable capability, or unverifiable security bindings.
- Prohibited native tool events and uncertain process stops.
- Owner pauses, required approval, and unknown failures that require action.

An active shared setup failure remains visible despite a newer contained failure.
If new work remains safe, these failures do not require owner action:

- Exhausted correction.
- Ordinary proposal rejection.
- Exhausted safe retries.

A fixed read limit remains available for inspection.
It does not request unchanged reruns.

### Fixed diagnostics

Record the observed failure phase with one of these fixed values:

- Native output.
- Model format.
- Candidate validation.
- Controlled read.
- Original evidence.
- Commit.

Retain more specific fixed error codes.
Keep transcript text, model text, raw validation messages, and secrets out of diagnostics.
For older records without a phase, show `not recorded`.
Do not infer one.

Use fixed `review_call.outcome` values for a completed call that requests correction.
Before the marker write, check the completed call, attempt, policy, and generation.
Preserve existing terminal-call, usage, and charge accounting.
A later launched correction call proves an attempt.
To prove exhaustion, also require the marker, completed correction call, and final failure.

Two calls alone cannot prove correction.
The first call can request a read.
If saved history is insufficient, use `unknown`.
These correction and diagnostic changes add no schema table or field.
