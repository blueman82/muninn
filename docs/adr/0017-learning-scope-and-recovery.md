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
Reindexing changed originals must invalidate the earlier confirmation.
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
Keep owner pauses unless the person separately approves a checked restart.
Recognise unchanged legacy defaults with their recorded runner version.
Require every recorded call and attempt to have finished before restart.
Keep uncertain processes blocked.
Confirm replacement requests without moving an existing From now start time.

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
Muninn cannot restore missing originals or approve changed security bindings.
A person must provide missing account access or approval when required.

Revisit worker count and history interval size using measured queue age, retries, and revision conflicts.

## Proposed amendment: bounded correction and action routing

Status: PROPOSED. Owner approval pending.
The accepted decision above remains in force, including “Reject deterministic
invalid results once.” This section proposes a narrow exception and presentation
changes. It does not record acceptance or authorize implementation.
See the [memory lifecycle design](../memory-lifecycle-design.md).
The separate [ADR 0018 proposal](0018-conversational-memory-decisions.md) covers
conversational owner decisions and their governed schema.

### Correction within the existing allowance

Permit at most one correction call when an existing review round remains.
Keep the default two rounds, three total attempts, four workers, deadlines,
permit accounting, and 5-second and 30-second transient backoffs unchanged.
A preceding controlled read can consume the remaining round.
The correction must return a final operation; it cannot request another read.

Allow only these two correction forms:

1. Format correction after native decoding and strict JSON decoding pass, but
   local union or schema validation fails. An unknown operation can qualify at
   this boundary. Do not trust its discriminator before validation succeeds.
2. Strict canonical subset pruning after a fully typed final fails an allowlisted
   candidate check. Remove at least one candidate and preserve the order and
   exact canonical form of every survivor. Apply this rule to changes and
   unresolved items. An empty result uses the fixed no-change outcome.

The candidate-failure allowlist is scope mismatch, missing direct-owner support,
a repeated replacement target, and overlap between replacement targets and
affected unresolved targets. Scope mismatch permits removal only.
Missing owner support permits removal only; it cannot authorize relabeling or
new support. All other authority failures remain terminal.
Do not add or rewrite text, kind, action, scope, target, revision, supports,
dependencies, expiry, claims, or unresolved items during pruning.

Never correct native framing or JSON failures, prohibited native tool events,
runner or containment failures, input/output limits, or invalid security bindings.
Keep controlled-read refusals, source-read limits, changed originals, erased or
unavailable evidence, unknown evidence references, stale or invalid targets,
policy changes, and generation-fence failures terminal for that result.
Do not classify safety failures through exception-text matching.

Use fixed program-owned format feedback or a typed allowlisted candidate failure
with a candidate position. Return no rejected text or raw exception messages.
Only the supervisor may defer an allowlisted candidate failure, after original
evidence passes and while a correction round remains.
Roll back the rejected transaction without governed writes or terminal attempt
bookkeeping. Other callers retain existing behavior.
Keep the original canonical batch in transient round state for comparison.

Before correction, refresh the active request and admitted evidence union.
Recheck policy, generation, rounds, original-read budgets, deadline, and knowledge
freshness. Repeat all current checks at the corrected commit.
Use no extra preflight evidence read, nested writer lock, or persisted rejected
model-content diagnostic. If bounded required feedback cannot fit, stop safely.

### Startup action and complete inspection

Add startup-only action selection from finite failure codes.
Keep ordinary contained request failures in doctor, request history, and explanations.
Do not require dismissal merely to stop repeated SessionStart presentation.
Preserve the existing `notice()` selection and doctor's complete attention semantics.

Startup still shows account access, unverifiable capability or security bindings,
prohibited native tool events, uncertain process stops, owner pauses, required
approval, and unknown action-required failures.
An active shared setup failure remains visible despite a newer contained failure.
Exhausted correction, ordinary proposal rejection, and exhausted safe retries do
not require owner action when admission remains safe.
A deterministic read limit remains inspectable and does not request unchanged reruns.

### Fixed diagnostics

Record the observed failure phase using fixed values: native output, model format,
candidate validation, controlled read, original evidence, or commit.
Retain more specific finite error codes. Keep transcript text, model text, raw
validation messages, and secrets out of diagnostics.
Show `not recorded` for older records without a phase; do not infer one.

Use finite `review_call.outcome` values for a completed call that requests correction.
Persist a marker only after checking the completed call, attempt, policy, and
generation fence. Preserve existing terminal-call, usage, and charge accounting.
A later launched correction call proves an attempt; exhaustion also requires the
marker, completed correction call, and final failure.
Two calls alone cannot prove correction because a first call can request a read.
Use `unknown` when durable history is insufficient.
These correction and diagnostic changes add no schema table or field.
