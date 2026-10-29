# 0017: Choose learning scope and recover bounded failures

## Status

Accepted by the owner on 26 October 2026.
This decision updates the default limits and refresh rules in ADR 0015.

## Context

One failed request previously stopped ongoing Codex learning admission.
A new user could not choose whether to review older records during installation.
A single worker also delayed large history runs.

## Decision

Offer From now and All available history during first setup.
Keep the selected scope across upgrades and rebuilds.
Verify history and show exclusions before confirmation.
Bind confirmation to all traversed original bytes and indexed records.
After Muninn reindexes changed originals, it must invalidate the earlier confirmation.

Use existing request identities to prevent duplicate admission.
Keep unverifiable sources excluded.

The new default uses policy revision 3.
It permits four workers and three total attempts per request.
Manual and live work take priority over queued history.
Keep creation order within each priority group.

Keep SQLite writes on the main thread.
Check current knowledge metadata inside the commit transaction.
Rebuild stale results within the same attempt limit.

Retry safe temporary failures with 5-second and 30-second backoffs.
Reject deterministic invalid results once.
Continue unrelated safe requests after a request fails.
Recheck unavailable setup every 60 seconds.
Keep owner pauses and unresolved process stops in force.
Preserve the existing evidence, authority, scope, erasure, and runner security checks.

Add `learning_setup` and `review_issue` as optional state tables.
Keep the existing marked store version and governed table shapes.
Pin both supported schema fingerprints for copy and rebuild operations.
Copy the optional tables when present.

The installer creates state inside its maintenance pause and snapshot protection.
Older maintenance tools can refuse the extended schema.
Rollback uses the retained store snapshot.

Refresh eligible repository defaults from revision 1 or 2 to revision 3.
Preserve old request policies and saved knowledge.
Mark unstarted old-policy requests as replaced.
Use the selected scope to admit fresh requests.

Preserve changed custom policies.
Keep owner pauses unless the person separately gives approval for a checked restart.
Recognize unchanged legacy defaults with their recorded runner version.

Require every recorded call and attempt to have finished before restart.
Keep uncertain processes blocked.
Confirm replacement requests. Do not move an existing From now start time.

Show required action at the next Codex session start.
Provide safe diagnostic detail through `learn show` and `doctor`.
Keep local HTML and video previews ignored.

## Consequences

Four workers can reduce elapsed time and consume provider allowance faster.
Concurrent knowledge changes can require fresh calls.
Three attempts limit one request's retries.
They do not limit account cost or subscription allowance.

Estimate whole worker batches from comparable recorded calls.
Separate first-attempt estimates from estimates with permitted retries and backoffs.
Exclude setup waits and database delays from these estimates.

Source verification can exclude history that exceeds the original-read limit.
The scan reports these exclusions with their causes.
Muninn cannot restore missing originals or give approval for changed security bindings.
A person must provide missing account access or approval when required.

Use measured queue age, retries, and revision conflicts to reconsider worker count and history interval size.

## Proposed amendment: bounded correction and action routing

Status: PROPOSED. Owner approval pending.
The accepted decision above remains in force, including “Reject deterministic invalid results once.”
This section proposes a narrow exception and changes to failure presentation.
It does not record acceptance or authorize implementation.

See the [memory lifecycle design](../memory-lifecycle-design.md).
The separate [ADR 0018 proposal](0018-conversational-memory-decisions.md) covers conversational owner decisions and their governed schema.

### Correction within the existing allowance

Permit at most one correction call when an existing review round remains.
Keep these limits unchanged:

- Two rounds by default, three total attempts, and four workers.
- Deadlines and permit accounting.
- The 5-second and 30-second transient backoffs.

A preceding controlled read can consume the remaining round.
The correction must return a final operation. It cannot request another read.

Allow only these two correction forms:

1. **Format correction:** Native decoding and strict JSON decoding pass, but local union or schema validation fails.
   An unknown operation can qualify at this boundary.
   Before validation succeeds, do not trust its discriminator.
2. **Canonical subset reduction:** A fully typed final fails an allowlisted candidate check.
   Remove at least one candidate.
   Preserve the order and exact canonical form of every remaining candidate.
   The canonical form is the change data after required normalization.
   Apply this rule to changes and unresolved items.
   An empty result uses the fixed no-change outcome.

Permit reduction only for these candidate failures:

- Scope mismatch.
- Missing direct-owner support.
- A repeated replacement target.
- Overlap between replacement targets and affected unresolved targets.

A scope mismatch permits removal only.
Missing owner support also permits removal only. It cannot authorize relabeling or new support.
All other authority failures remain terminal.
During reduction, do not add or rewrite any of these fields:

- Text, kind, action, scope, target, or revision.
- Supports, dependencies, expiry, claims, or unresolved items.

Never correct these failures:

- Native framing or JSON failures.
- Prohibited native tool events, runner or containment failures.
- Input/output limits or invalid security bindings.

Keep these failures terminal for that result:

- Controlled-read refusals and source-read limits.
- Changed originals, erased or unavailable evidence, and unknown evidence references.
- Stale or invalid targets, policy changes, and generation-fence failures.

Do not classify safety failures through exception-text matches.
Use fixed program-owned format feedback or a typed allowlisted candidate failure with a candidate position.
Return no rejected text or raw exception messages.

Only the supervisor may defer an allowlisted candidate failure.
Deferral requires successful original-evidence checks and a remaining correction round.
Roll back the rejected transaction without governed writes or terminal attempt bookkeeping.
Other callers retain existing behavior.
Keep the original canonical batch in transient round state for comparison.

Before correction, refresh the active request and admitted evidence union.
Recheck policy, generation, rounds, original-read budgets, deadline, and knowledge freshness.
Repeat all current checks at the corrected commit.
Use no extra preflight evidence read, nested writer lock, or persisted rejected model-content diagnostic.
If bounded required feedback cannot fit, stop safely.

### Startup action and complete inspection

Add startup-only action selection from finite failure codes.
Keep ordinary contained request failures in doctor, request history, and explanations.
Do not require dismissal merely to stop repeated SessionStart presentation.
Preserve the existing `notice()` selection and every issue that doctor reports as requiring attention.

Startup still shows these failures or required actions:

- Account access, unverifiable capability or security bindings.
- Prohibited native tool events and uncertain process stops.
- Owner pauses, required approval, and unknown action-required failures.

An active shared setup failure remains visible despite a newer contained failure.
If admission remains safe, these failures do not require owner action:

- Exhausted correction.
- Ordinary proposal rejection.
- Exhausted safe retries.

A deterministic read limit remains inspectable. It does not request unchanged reruns.

### Fixed diagnostics

Record the observed failure phase with one of these fixed values:

- Native output.
- Model format.
- Candidate validation.
- Controlled read.
- Original evidence.
- Commit.

Retain more specific finite error codes.
Keep transcript text, model text, raw validation messages, and secrets out of diagnostics.
For older records without a phase, show `not recorded`. Do not infer one.

Use finite `review_call.outcome` values for a completed call that requests correction.
Before the marker write, check the completed call, attempt, policy, and generation fence.
Preserve existing terminal-call, usage, and charge accounting.
A later launched correction call proves an attempt.
Exhaustion also requires the marker, completed correction call, and final failure.

Two calls alone cannot prove correction because a first call can request a read.
If durable history is insufficient, use `unknown`.
These correction and diagnostic changes add no schema table or field.
