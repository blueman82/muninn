# 0017: Choose learning scope and recover bounded failures

## Status

Accepted by the owner on 26 October 2026.
This decision updates the default limits and refresh rules in ADR 0015.

## What this decision does

Muninn reads conversation records and proposes useful facts or instructions to keep.
It checks each proposal before it records knowledge.
One unsuccessful proposal must not stop other safe work.
A small, safe correction can use a `call` that the `job` permits.

This decision also lets the owner select which conversations Muninn can examine.
Ordinary safe learning does not make approval necessary for each memory.
[ADR 0018](0018-conversational-memory-decisions.md) includes the different conditions that make an owner decision necessary.

## Terms

- **Proposal:** A change that Muninn has not accepted as recorded knowledge.

- **`Evidence`:** An `original` conversation record that supplies `support` for a statement.

- **`Job`:** A `request` for Muninn to examine specified `evidence` in fixed limits.
  The software names it a `request`.

- **`Attempt`:** One try to complete a `job`.
  A temporary error can make one more `attempt` possible.

- **`Round`:** One model `call` in an `attempt`.
  A `round` can specify an allowed `evidence` read or report a last proposal.

- **Worker:** A `process` that runs one `job`.

- **`Review` range:** The selection of which conversations Muninn can examine.
  `From now` and `All available history` are `review` ranges.

- **Knowledge scope:** The global, project, and optional task limits that control where knowledge can apply.
  Subsequent `references` to scope mean knowledge scope unless they explicitly say `review` range.

- **Revision:** A numbered version of a recorded item.

- **Transaction:** A database operation that records all its changes together or records none.

- **Generation check:** Muninn records a current work version number, called a generation.
  Each `review` `attempt` keeps the generation number it started with.
  When the current number changes, older `attempts` cannot record.
  The generation check compares the attempt's number with the current number.

  The software also names this a generation fence.

- **Schema fingerprint:** A check of the database table structure against a known accepted structure.

## Context

Previously, one unsuccessful `request` stopped Muninn from accepting new ongoing Codex learning `jobs`.
A new user could not select older records for `review` during installation.
A single worker also increased the time for large history `jobs`.

## Decision

### Choose and preserve the review range

Offer `From now` and `All available history` during first setup.
Keep the selected `review` range across upgrades and rebuilds.
Before confirmation, check the history.
Show excluded records before confirmation.

Connect confirmation to each `original` byte and indexed record that the scan reads.
After Muninn indexes changed `originals` again, invalidate the earlier confirmation.

Use current `request` identities to prevent duplicate `jobs`.
Do not include sources that Muninn cannot check.

### Limit calls and concurrent work

Use policy revision 3 as the new default.
Use at maximum four workers and three total `attempts` for each `request`.
Do manual and live work before queued history.
Keep creation sequence in each of those groups.

Keep SQLite writes on the primary thread.
In the record transaction, check current knowledge metadata.
If results become stale, prepare new results in the same `attempt` limit.

For safe temporary errors, wait 5 seconds and then 30 seconds before retries.
Reject a result with an unchanged invalid condition one time.
After a `request` fails, continue unrelated safe `requests`.
Check unavailable setup again at 60-second intervals.

Keep owner pauses and unresolved `process` stops in force.
Keep all current `evidence`, authority, scope, erasure, and runner `security` checks.
Authority checks check who can authorize a change.

### Preserve storage and recovery protection

Add `learning_setup` and `review_issue` as optional `state` tables.
Keep the current marked store version and necessary table structures for this unit.
ADR 0018 independently governs the subsequent database-format change.
For copy and rebuild operations, keep the two accepted schema fingerprints.

Copy the optional tables when the database has them.

The installer makes `state` while maintenance pauses usual work.
A store snapshot protects that change.
Older maintenance tools can refuse the extended schema.
Rollback uses the kept store snapshot.

### Refresh eligible defaults

Refresh unchanged repository defaults from revision 1 or 2 to revision 3.
Keep previous `request` policies and recorded knowledge.
Mark unstarted `requests` with previous policies as replaced.
Use the selected `review` range to make new `requests`.

Keep custom policies that someone changed.
Keep owner pauses unless the person independently authorizes a checked restart.
Use the recorded runner version to identify unchanged legacy defaults.

Each recorded `call` and `attempt` must have finished before restart.
If a `process` might run, keep it blocked.
Get confirmation for replacement `requests`.
Do not move a current `From now` start time.

At the next Codex session start, show necessary `action`.
Give safe diagnostic information through `learn show` and `doctor`.
Keep local HTML and video previews ignored by Git.

## Consequences

Four workers can decrease elapsed time.
They can also consume the provider allowance faster.
Concurrent knowledge changes can make new `calls` necessary.
Three `attempts` limit retries for one `request`.

They do not limit account cost or subscription allowance.

Estimate full worker batches from comparable recorded `calls`.
Show first-attempt estimates independently from estimates with permitted retries and waits.
Do not include setup or database waits in these estimates.

Source verification can skip history above the original-read limit.
The scan reports these exclusions and their causes.
Muninn cannot put back missing `originals` or authorize changed `security` settings.
A person must give missing account access or approval when necessary.

Use measured queue age, retries, and revision disagreements to reconsider worker count and history interval capacity.

## Proposed amendment: bounded correction and action routing

Status: ACCEPTED by the owner on 2026-10-29.
The source contains the unit 1 change.
Unit 1 was merged in PR #77 at `5fa95b51e5628ca52082da846d2746473cbc961b`.
Installation and live verification stay different `delivery` checks.

The conversational decisions in ADR 0018 make a different unit.
The private source tree contains that unit.
New `review`, its last full gate, publication, installation, and live verification stay pending.
Owner authorization: “all above is approved what questions would you ask”.

The accepted decision above stays in force with this narrow correction and presentation change.
This approval records authority to change the project.
It does not prove an owner's decision about a runtime memory proposal.

See the [memory lifecycle design](../memory-lifecycle-design.md).
The different [ADR 0018 proposal](0018-conversational-memory-decisions.md) includes owner decisions and their database records.

### Correction within the existing allowance

Use at maximum one correction `call` when a current `review` `round` stays.
Keep these limits unchanged:

- Two `rounds` by default, three total `attempts`, and four workers.

- Deadlines and permit accounting.

- The 5-second and 30-second waits for temporary errors.

A preceding controlled read can consume the remaining `round`.
A controlled read lets the model `request` only `evidence` that the `job` permits.
The correction must report a `final` operation.
It cannot select one more read.

The model runner is the software that starts the model and reports its response.
The native decoder reads that response format.
The JSON decoder reads the structured data in the response.
The schema validator checks the permitted operation and its necessary data fields.

The supervisor is Muninn's software that controls the `review` `job`.
A correction child is a new model `process` that the supervisor starts for a correction.

Use only these two correction types:

1. **Format correction:** The native decoder and strict JSON decoder accept the output.
   The local union or schema validator then rejects its structure.
   These validators check the permitted operation and necessary data fields.
   An unknown operation can qualify at this boundary.

   Before validation succeeds, do not trust the field that selects the operation.

2. **Subset reduction:** A fully typed `final` result fails one of the candidate checks listed below.
   A fully typed result satisfies schema validation.
   A candidate is one proposed change or unresolved item in that result.

   Remove at minimum one candidate.
   Keep each remaining candidate in its `original` sequence and unchanged normalized representation.
   Normalization means the necessary setup of data before data is compared or stored.
   Apply this rule to changes and unresolved items.

   For an empty result, use the fixed no-change outcome.

Use reduction only for these candidate errors:

- Scope mismatch.

- Missing direct-owner `support`.

- A duplicate replacement target.

- Overlap between replacement targets and affected unresolved targets.

A target is the recorded item that a proposal would replace or affect.
Direct-owner `support` means checked `original` `evidence` from the owner.
An unresolved item records a question that Muninn cannot safely settle.

A scope mismatch permits removal only.
With missing owner `support`, removal is also the only permitted change.
It cannot authorize a different `kind` of knowledge or new `support`.
All other authority errors end that result.

During reduction, do not add or rewrite these fields:

- Text, `kind`, `action`, scope, target, or revision.

- `Supports`, dependencies, expiry, claims, or unresolved items.

Do not correct these errors:

- Native framing or JSON errors.

- Native tool `events` that the policy rejects, runner errors, or errors in `process` containment.

- Input/output limits or invalid `security` bindings.

A `security` binding connects a `call` to its approved host, policy, and launch input.
These errors end that result without correction:

- Controlled-read refusals and source-read limits.

- Changed `originals`, erased or unavailable `evidence`, and unknown `evidence` IDs.

- Stale or invalid targets, policy changes, and unsuccessful generation checks.

### Give fixed feedback and retain no rejected draft

Do not classify safety errors from `exception` text.
For format correction, use constant feedback that the software controls.
For reduction, use a typed permitted error with the candidate's position.
Feedback contains only constant codes and candidate coordinates.

It contains no rejected text or raw `exception` messages.

Give the new correction child constant instructions that permit a `final` result only.
For reduction, give removal-only rules.
Give this unchanged empty-final structure:

```json
{"op":"final","result":{"schema_version":1,"changes":[],"unresolved":[],"no_change_reason":"No supported candidates remain after correction."}}
```

For reduction, send the fully typed normalized `original` batch independently as untrusted temporary prompt data.
The software names this `original` batch the canonical baseline.
Its content cannot become trusted instructions, `evidence`, or owner authority.
Do not keep raw output with an invalid schema.

Only the supervisor may defer a permitted candidate error for correction.
First, all original-evidence checks must succeed, and a correction `round` must stay available.
Roll back the rejected transaction without governed writes or last `attempt` records.
`Governed` writes are changes subject to Muninn's validation, maintenance, and erasure rules.

Other callers keep current behavior.

Keep the `original` normalized batch in temporary `round` `state` to compare unchanged values independently.
Send its normalized representation only to the next correction child as untrusted data.
Do not record this batch in history, `call` markers, diagnostics, logs, or receipts.
Only independently accepted knowledge follows the usual retention rules.

After recovery, block the previous `attempt`.
Do not reconstruct a batch that is unavailable.

### Recheck safety before correction and save

Before correction, refresh the active `request` and the complete set of `evidence` admitted for it.
Check again policy, generation, `rounds`, original-read budgets, deadline, and knowledge freshness.
At the corrected record, run again all current checks.
Use no more preflight `evidence` read.

Do not get the database write lock again while the outer operation holds it.
Do not record rejected model content as diagnostic data.

Count the complete UTF-8 prompt against the current `max_input_bytes` cap.
Include constant instructions, feedback, and the `original` batch.
If the prompt cannot fit, fail `prompt_limit` before one more reservation or launch.
Do not truncate the `original` batch or omit necessary context.

Do not increase limits or add `calls`.

### Startup action and complete inspection

Select startup steps from constant error codes.
Keep ordinary safely contained `request` errors in doctor, `request` history, and explanations.
Do not make dismissal necessary merely to stop duplicate SessionStart notices.
Keep the current `notice()` selection.

Keep each issue that doctor reports as needing owner input.

Startup shows these errors or necessary steps:

- Account access, unverifiable `capability`, or unverifiable `security` bindings.

- Native tool `events` that the policy rejects and uncertain `process` stops.

- Owner pauses, necessary approval, and unknown errors that need `action`.

An active shared setup error stays readable despite a newer contained error.
If new work stays safe, these errors do not make owner steps necessary:

- Exhausted correction.

- Ordinary proposal refusal.

- Exhausted safe retries.

A constant read limit stays available for checks.
It does not `request` unchanged reruns.

### Fixed diagnostics

Record the measured error phase with one of these constant values:

- Native output.

- Model format.

- Candidate validation.

- Controlled read.

- `Original` `evidence`.

- Commit.

Keep more specified constant error codes.
Keep transcript text, model text, raw validation messages, and secrets out of diagnostics.
For older records without a phase, show `not recorded`.
Do not infer one.

Use fixed `review_call.outcome` values for a completed `call` that specifies correction.
Before the marker write, check the completed `call`, `attempt`, policy, and generation.
Keep current terminal-call, use, and charge accounting.
A subsequent launched correction `call` proves an `attempt`.

Proof of exhaustion also makes the marker, completed correction `call`, and terminal error necessary.

Two `calls` alone cannot prove correction.
The first `call` can specify a read.
If recorded history cannot prove it, use `unknown`.
These correction and diagnostic changes add no schema table or field.
