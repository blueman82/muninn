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
