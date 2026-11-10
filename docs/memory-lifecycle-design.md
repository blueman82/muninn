# Improve how Muninn checks and saves memory

Status: APPROVED by the owner on 2026-10-29.
The source implements the commands, checked application, maintenance, and erasure.
A source change does not prove the selected release, local installation, or live verification.
Owner authorization: “all above is approved what questions would you ask”.

This approval gives authority to build the design.
It does not prove an owner's decision about a runtime memory proposal.

The owner specified conversational approval, edit, and refusal operations.
The owner delegated the selection of work schedule to the implementer.
The implementer is the person or agent that builds this feature.
The approval includes this revised design and the two ADR changes.

## How it will work

1. A session ends.
   Muninn can then `review` its records without an active conversation.

2. Muninn prepares proposed facts or instructions and checks their `evidence`.
   Ordinary safe learning records automatically.

3. Sometimes the `evidence` leaves an actual question that Muninn cannot safely answer.
   The agent shows it only when it is related to the current task or the owner specifies a `review`.

4. The agent prepares a complete proposed memory change.
   It shows the proposed content, each record marked for replacement, and the proposed answer to a question.
   That same display gives the unchanged reply sentences for this proposal.

5. The owner replies in conversation.
   The agent checks the `original` reply and runs the commands.

6. Muninn checks the change again before it records anything.
   The agent reports recorded, pending, rejected, or blocked from the actual result.

Commands, database queries, JSON, and hashes are not necessary for the owner.
A view gives no approval.
An edit makes a new proposal.
A new display and its own approval are necessary.
A usual unsuccessful proposal does not automatically become an owner question.

This design also includes one narrow correction in the job's current `call` allowance.
Muninn can correct a format error or remove specified unsupported items.
It cannot rewrite a rejected preference as a fact to evade a safety check.
If safe correction is unsuccessful, other safe `jobs` can continue.

## What is complete

The accepted ADR 0017 decision stays in force.
This revision changes its explanation, not its authority or date.

Implementation baseline: Muninn `a5ff0733a5ba5ec37febacd5445e4c10fc59b684`.
That baseline includes the checked PR #76 context fix.
Initial research used `56d1977`.
The `gap` descriptions below report that inspected baseline.

These claims apply only to the inspected baseline.

Unit 1 includes sections A–C and the added procedure tests.
The source contains it.
Unit 1 was merged in PR #77 at `5fa95b51e5628ca52082da846d2746473cbc961b`.
Installation and live verification stay different `delivery` checks.

Section D and ADR 0018 make unit 2.
The source contains that unit.
Its commands connect complete unchanged displays and `original` owner replies to checked application and receipts.
Maintenance and erasure include its governed records.

A source change does not prove the selected release, local installation, or live verification.

The Hermes source version is commit `fcc26fbd9f1641755e49793d7ed608f3b7214c3d`, inspected on 29 October 2026.
The HyperSkill source version is a clean checkout at `a34978aa7439d2eff7ccc3d2ad5372b34d285bd5`.
These source checks report those source versions.
They do not prove live behavior on one or the other system.

## Proposed changes

1. Use at maximum one safe correction `call` when a current `review` `round` stays.
   Keep two `rounds`, three `attempts`, four workers, and all `evidence` checks.

2. Use format correction or candidate removal only after the specified errors.
   Keep each remaining candidate unchanged as Muninn originally normalized it.

3. Stop duplicate SessionStart notices for `request` errors that Muninn safely contained.
   Keep complete diagnostics and history.
   Keep startup notices for actual owner `action`.

4. Record constant error phases and correction outcomes in current tables.
   No new table, provider, database, or daemon is necessary for these changes.

5. Show related unresolved questions and complete memory proposals in conversation.
   Connect approval to the unchanged displayed proposal.
   Include subsequent checked background application.

6. Add procedure tests in Muninn's current `evaluation` framework.
   Test success with condition A, a paraphrase that keeps A, and a related task without A.
   Test an unsuccessful run with an unknown cause and duplicate recalled advice.
   Recalled advice alone must not become `independent` `evidence`.

   An unknown error cause must not become a proved pitfall.

The procedure tests add coverage.
They do not prove a runtime defect or measured benefit.

## Purpose

Decrease avoidable errors and duplicate owner work when Muninn checks memory proposals.
Keep original-source checks, author checks, scope, authority, and erasure rules in force.
Use Hermes and HyperSkill as sources for demonstrated `gaps`.
Add only the needed changes.

Muninn should correct safe errors in its current allowance.
If correction fails, the owner can examine the result.
Ordinary error notices do not make duplicate dismissal necessary.
Errors that make account access, `security` steps, or approval necessary must stay readable.

## Terms used in this proposal

- **Proposal:** A change that Muninn has not accepted as recorded knowledge.
  An owner proposal is a complete memory change for the owner to examine.
  The software names it a candidate.

- **`Evidence`:** An `original` conversation record that supplies `support` for a statement.

- **`Job`:** A `request` to examine specified `evidence` or apply a checked change in fixed limits.

- **Unresolved question:** A recorded question that Muninn cannot safely settle from its current `evidence`.

- **Candidate:** One proposed change or unresolved item in a model's `final` result.
  A correction can remove a candidate only with the rules below.

- **`Round`:** One model `call` in an `attempt` to complete a `job`.

- **`Support` ID:** The ID of `evidence` that supplies `support` for a claim.

- **Normalized change data:** The change data after Muninn's necessary setup for storage or equality checks.
  The software names this the canonical payload.

- **Owner proof:** An `original` owner statement with source and author identities that Muninn can check.

- **Digest:** A hash that connects an identity to the same stored data.
  A label specified to the record type keeps different record types different.

- **Receipt:** The durable record that confirms which checked knowledge changes committed.

- **Transaction:** A database operation that commits all its changes together or rolls them all back.

- **Scope:** The global or project boundary for a `request` or knowledge item.
  A project scope can also have a task limit.

- **Target:** A database record and revision that a proposal would replace or affect.

- **Revision:** A numbered version of a record.

- **Generation check:** Muninn records a current work version number, called a generation.
  Each `review` `attempt` keeps the generation number it started with.
  When the current number changes, older `attempts` cannot record.
  The generation check compares the attempt's number with the current number.

  The software also names this a generation fence.

- **Writer root:** The outer operation that holds the shared lock for database writes.
  It cannot get that same lock again while it holds it.

- **Owner-support rule:** The disclosed rule that adds checked owner-decision `evidence` to `original` `support` IDs.
  The software names this `support` enrichment.

For example, one project record says Python 3.12.
A subsequent record says Python 3.13.
If the `evidence` cannot settle the difference, Muninn keeps a question.
The agent can prepare a complete proposal to use Python 3.13 for that project.

Apply the usual `evidence` and owner checks to that proposal.

Global scope is selected explicitly.
Ordinary project reads do not inherit global guidance.
A project scope applies to the specified project.
An optional task limit can limit it more.

The identifiers below name records:

| Identifier | Record |
| --- | --- |
| `D_ID` | A complete proposal for an owner decision. |
| `E_ID` | An `evidence` record. |
| `R_ID` | A learning `request` or application `job`. |
| `U_ID` | An unresolved question. |

These are placeholders for actual IDs.
The agent gives the owner reply sentences with the actual proposal ID.

**Headless learning** runs without an active owner conversation.
**Foreground work** starts in a live owner conversation.
Background execution can finish it subsequently.

**`Governed` data** stays subject to Muninn's validation, maintenance, and erasure rules.
**Admission** means permission to make or run a `job` with the current policy.
**`Security` bindings** connect work to its approved host, policy, and launch input.
A **tombstone** prevents erased content from coming back.

## Existing lifecycle and reference patterns

### Read conversation records

Muninn reads provider records without changing them.
It has transcript search, `event` reads, and session views.
Hermes also has `search` followed by reads with more information.
Keep Muninn's current paths.

Do not add a different archive, vector index, or search daemon.

### Select knowledge to retain

Muninn notes hold facts with checked `support`, preferences, decisions, and other knowledge types.
`Guides` hold procedures and lessons.
Hermes puts user facts in `USER.md` and environment facts in `MEMORY.md`.
Task procedures belong in skills.

Keep Muninn's note and `guide` model.
Do not add duplicate user or environment files.

### Create a review job

SessionEnd, PreCompact, reconciliation, and checked history selection make bounded `jobs`.
History confirmation binds the `original` bytes.
Hermes checks conversation snapshots after a response.
Its explicit `/refine` `review` is different from automatic `review`.

Keep Muninn's current triggers and fixed `request` identity.
Do not add a different `review` worker.

### Prepare, check, and save a proposal

Muninn has controlled `evidence` reads and `final` results in a constant number of `rounds`.
At the inspected baseline, an invalid result ends the `attempt`.
Hermes can report correction information after memory-tool errors.
Add only the narrow correction below, in a current remaining `round`.

Before writing, Muninn checks `original` bytes, permitted `evidence`, authority, scope, current revisions, and tombstones.
It records correct changes in one transaction.
Hermes checks `final` batch capacity and fixes approval to a selected entry.
Keep all Muninn checks in its write path.

Shared candidate checks must not bypass the transaction.

### Supply context to later work

Muninn startup includes complete eligible notes and a compact `guide` index.
Prompt recall selects scoped context in byte limits.
Hermes uses small memory blocks and fixed prompt snapshots.
It refreshes those snapshots after context compression.

Keep Muninn's bounded, scoped `delivery`.
Change prompt snapshots only after a measured freshness or cache problem.

### Maintain and inspect knowledge

Muninn handles revisions, `conflicts`, questions, expiry, dependencies, tombstones, and erasure.
Hermes can add, change, and remove memory.
Its external providers have different lifecycle hooks.
Keep Muninn's current maintenance rules and default automatic learning.

Add the missing path for related owner decisions.

At the inspected baseline, Muninn read commands show unresolved IDs or counts without question content.
Hermes has commands to examine pending proposals.
Add scoped checks and complete conversational decisions without new owner authority.

### Explain failures

Muninn records safe codes for `learn show`, explanations, and doctor.
Startup selects a recorded issue.
Hermes distinguishes selected pending proposals from completed writes in `review` notices.
Show startup `action` only when needed.

Keep ordinary safely contained errors available for checks.

Muninn source anchors: [knowledge models](../muninn/knowledge_models.py), [bounded startup context](../muninn/knowledge_push.py), [scoped recall](../muninn/hook_recall.py), [capture](../muninn/hook_learn.py), [history admission](../muninn/review_history.py), [`original` `evidence`](../muninn/review_evidence.py), [atomic commit](../muninn/review_commit.py), and [erasure](../muninn/knowledge_erase.py).

These are comparable functions.
Their policies, coverage, and live verification can be different.

## Gaps confirmed in the inspected source

### 1. Safe correction stops too early

`review_supervisor_result.execute` sends model parsing errors directly to the error callback.
It also maps native output errors to a broad refusal code.
The current `round` mechanism continues after a checked read, but cannot report safe validation feedback for correction.

Use only the correction conditions defined below.
Do not let a model rewrite a rejected claim again and again until it satisfies the checks.
For example, authority checks use the declared knowledge `kind` as one input.
A correction without the specified restrictions could relabel a preference as a fact after an authority refusal.

That would bypass the authority check.

Sources: [result reconciliation](../muninn/review_supervisor_result.py), [`round` validation](../muninn/review_rounds.py), and [candidate authority checks](../muninn/review_commit.py).

### 2. Safely contained failures can become repeated owner reminders

`review_recovery.notice` selects the setup issue first, then the latest recorded `request` issue.
It does not distinguish a `request` that safely ended from an error that needs owner `action`.
SessionStart can show an ordinary model refusal again until someone dismisses its record.
Keep the diagnostic record.

Change when SessionStart shows it.

Sources: [issue selection](../muninn/review_recovery.py) and [SessionStart use](../muninn/hook.py).

### 3. Failure records lose the relevant phase

Result reconciliation groups parsing, controlled reads, `evidence` checks, and commit refusal into broad error handling.
The supervisor callback defaults the diagnostic `stage` to `review`.
The broad label hides if the error came from model formatting, `evidence` checks, or storage.
Some older `attempts` have no recorded `stage` with this information.

Sources: [result reconciliation](../muninn/review_supervisor_result.py), [supervisor error recording](../muninn/review_supervisor.py), and [historical information fallback](../muninn/review_recovery.py).

### 4. Stored unresolved questions cannot be read through ordinary commands

`save_unresolved` stores the question, disputed claim, affected revisions, and `support` IDs.
Startup supplies unresolved IDs.
`Request` views expose receipt IDs or counts.
At the inspected baseline, the knowledge parser and handler have no `U_ID` read path.

The owner must be able to read a withheld decision without direct database access.

The current `unresolved_ids(scope)` query is deliberately narrower than a general question list.
It finds open questions that affect current applicable revisions.
It does not include permitted questions with no affected revisions.
A new read path must check the originating request's scope.

It must not use the narrower query as its authorization boundary.

A read alone does not complete the specified approval, edit, or refusal of memory in this conversation.
The conversational decision rules below fill that `gap` while ordinary learning stays automatic.

Sources: [question persistence](../muninn/knowledge_write.py), [current relevance query](../muninn/knowledge_read.py), [knowledge parser](../muninn/cli_parser.py), and [knowledge handler](../muninn/cli_know.py).

## Proposed behavior

### A. Bounded proposal correction

Use the request's current `max_rounds` allowance.
The default stays two `rounds`.
Do not make one more `attempt` or extend the deadline.
Do not increase worker count or change `request` scope.

If no `round` stays, record the safely contained error.
Continue unrelated `requests`.

The model runner is the software that starts the model and reports its response.
The native decoder reads that response format.
The JSON decoder reads the structured data in the response.
The schema validator checks the permitted operation and its necessary data fields.

The supervisor is Muninn's software that controls the `review` `job`.
A correction child is a new model `process` that the supervisor starts for a correction.

There are two different correction conditions:

1. **Format correction:** Native decoding and strict JSON decoding pass.
   Local union or schema validation then rejects the structure.
   These validators check the permitted operation and necessary data fields.
   Until validation succeeds, do not trust `op` or name the data a `final` result.

   Unknown operations and invalid schema structures qualify for constant software-owned feedback.

   The next operation must report a correct `final` result in the remaining last `round`.
   Native framing errors, invalid payload JSON, native tool `events`, oversized output, and runner errors do not qualify.

2. **Proposal reduction:** A fully typed `final` result fails one of the permitted candidate checks below.
   A fully typed result satisfies schema validation.
   The corrected result can remove candidates or report no change.
   Each remaining candidate must equal its `original` normalized representation.

   It cannot change text, `kind`, `action`, scope, target, revision, `evidence` IDs, dependencies, or expiry.
   It cannot introduce a new claim or unresolved item.

Reduction permits removal only.
It cannot expand the batch's meaning or authority.
Remove at minimum one candidate.
Keep the `original` sequence and unchanged normalized representation of each remaining candidate.

Apply this restriction to changes and unresolved items.
For an empty result, use the fixed no-change `reason`.

Report a typed candidate position and a constant error code.
Do not report arbitrary `exception` text.
Do not copy rejected model text into trusted instructions or logs.
Give the new correction child constant instructions that permit a `final` result only.

Give this unchanged empty-final structure:

```json
{"op":"final","result":{"schema_version":1,"changes":[],"unresolved":[],"no_change_reason":"No supported candidates remain after correction."}}
```

For reduction, send the fully typed normalized `original` batch independently as untrusted temporary prompt data.
The software names this `original` batch the canonical baseline.
It is material for equality checks.
It is not `evidence`, owner authority, or instructions.
Instructions in candidate text stay data.

They cannot change the correction rules.

Use correction only for these candidate errors:

- Candidate scope mismatch.

- Missing direct-owner `support`.

- A duplicate replacement target.

- Overlap between replacement targets and affected unresolved targets.

A scope mismatch permits removal of that candidate only.
It gives no access to a wider scope.
An authority error gives no permission for a different knowledge `kind` or new `support` IDs.
Other authority errors stay terminal for that result.

Terminal means the error ends that result without correction.

These errors also stay terminal:

- Changed `originals`, erased `evidence`, and unavailable `evidence`.

- Invalid read scope and uncertain `process` `state`.

- Broken `security` bindings.

- Unknown `evidence` IDs and invalid targets.

- Stale target revisions, policy changes, and unsuccessful generation checks.

An evidence-read limit is not a memory-capacity error.
Do not retry it unchanged.

Use a typed `ProposalRejectedError` only for the permitted candidate errors in the current validation path.
After all original-evidence checks pass, the supervisor can defer that typed error for correction.
Add a default-off `defer_candidate_failure` option to the current commit path.
Only the supervisor has permission to turn it on, and only when one correction `round` stays.

Roll back the rejected transaction without governed writes or last `attempt` records.
All other callers and errors keep their current behavior.

Keep the `original` candidate batch in temporary `Round` `state` for an `independent` check of the unchanged subset.
Send its normalized representation only to the next correction child as untrusted data.
Do not keep raw output with an invalid schema.
Do not record the `original` batch in history, `call` markers, diagnostics, logs, or receipts.

Only independently accepted knowledge follows the usual retention rules.
After restart, block the previous `attempt`.
Do not reconstruct a batch that is unavailable.

At corrected commit, run again all current checks against recorded `state` and `original` bytes.
Use no more preflight `evidence` read.
Do not get the write lock again while the outer operation holds it.

Use at maximum one correction `call`.
A preceding read `round` can consume that available `round`.
The correction must report a `final` operation.
It cannot specify one more controlled read.

Identify format correction at the decoding/schema boundary.
Identify reduction through its typed candidate error.
Do not use `exception` text to change a controlled-read or commit safety refusal into format feedback.

Before each correction `call`, refresh the active `request` and the complete set of `evidence` admitted for it.
Check again policy, generation, `round` allowance, original-read budgets, deadline, and knowledge freshness.
Keep the same permit and `attempt` accounting.

The current `max_input_bytes` cap includes the complete UTF-8 prompt.
It includes constant instructions, feedback, and the temporary `original` batch.
If the complete prompt cannot fit, stop with `prompt_limit` before one more reservation or launch.
Do not truncate the `original` batch or omit necessary context.

Do not increase limits or add `calls`.

### B. Owner-action selection

When issues are read, use a fixed code policy to classify them.
Use again the current issue table and inspection commands where possible.
A contained `request` error stays available through `learn show` and its explanation.
It does not make dismissal necessary to keep subsequent SessionStart hooks quiet.

Keep these `states` readable at startup:

- Sign-in or account access is necessary.

- Muninn cannot check an approved `capability`, host, launch input, or policy binding.

- A `review` reported a prohibited native tool `event`.

- Muninn cannot prove that a previous `process` stopped.

- Learning stays paused or needs a current owner approval `action`.

- Muninn cannot safely classify an unknown error as contained.

If new work stays safe, ordinary proposal refusal does not make owner steps necessary.
The same applies to exhausted format correction and exhausted safe retries.
A fixed source-read limit stays available for checks with its cause and maintainer `action`.
It must not become a duplicate `request` to rerun unchanged work.

Do not hide an active shared setup error because a newer `request` error exists.

Add an `action` selector used only at startup.
Keep the current `notice()` selection.
Keep each issue that doctor reports as needing owner input.
Usual errors stay readable in doctor, `request` history, and explanations.

Do not report doctor as healthy merely because startup stopped a duplicate notice.

Derive presentation from recorded `state`.
This avoids a new `delivery` ledger, expiry policy, or dismissal queue.

### C. Safe phase diagnostics

Record one constant phase for each new error:

- Native output.

- Model format.

- Candidate validation.

- Controlled read.

- `Original` `evidence`.

- Commit.

If a current constant error code gives one more specified cause, keep it.
Do not put transcript text, model content, raw validation messages, or provider secrets in diagnostics.

Show the phase through current `request` information and explanation output.
Use `not recorded` for previous `attempts` without `evidence`.
Do not infer historical phases.

Use constant software-owned `review_call.outcome` values to mark a completed `call` that specified correction.
First check the completed `call`, current `attempt`, policy, and generation.
Then record the marker in one transaction.
Keep current terminal-call `state`, use, and charge accounting unchanged.

A subsequent launched correction `call` proves a correction `attempt`.
Proof of exhaustion also makes the marker, completed correction `call`, and terminal error necessary.
Two `calls` alone do not prove correction.
The first `call` can specify a controlled read.

If recorded history cannot prove the distinction, use `unknown`.

Add no schema field or table.
Temporary `Round` data controls checks of the live subset.
It cannot prove historical correction.

### D. Unresolved question inspection and conversational decisions

#### Read the saved question without changing it

Add `muninn know unresolved [U_ID] [--human]`.
Without an ID, list applicable unresolved records in a bounded page.
With an ID, show the stored question, claim, affected revisions, `support` IDs, and originating `request`.
Default output is JSON.

The `--human` option gives an explanation for a person.
Read operations must not change an unresolved record's content or `state`.

Use the originating request's `scope_json` to check list access with current project/global rules.
Include records with an empty affected list.
Do not infer their scope from affected knowledge alone.

An explicit `U_ID` opens that unchanged historical record, as `know show` does for knowledge IDs.
It gives no permission for more `evidence` reads and gives no assurance of current `support`.
For an erased record or null `item_json`, report safe metadata without a body.
Do not reconstruct erased questions from receipts, `evidence`, or previous model output.

Apply current redaction and output `bounds`.
For malformed records, fail safely without raw stored content.
Keep question and claim content out of logs, doctor, and automatic startup notices.
For an unknown ID, report a safe not-found response.

Distinguish current relevance from historical `state`.
A checked replacement can activate revision N+1 of a withheld target.
A question that affects only revision N no longer withholds that replacement.
The `original` question can stay historically open.

Mark its affected revision stale.
Do not report automatic resolution.

A view gives no approval, new `evidence`, authority, or knowledge change.
The question itself stays a model proposal.
The owner gives approval, `requests` an edit, or rejects the complete proposal in conversation.
The agent runs all necessary commands.

Do not tell the owner to examine the database or run commands.

#### Selection and scheduling

Keep ordinary automatic learning free of a new approval requirement.
The `SessionEnd` hook can make a `job` for subsequent `review` without an active conversation.
Do not make an owner decision necessary for each learned item or safely contained proposal error.

Show a decision for an actual unresolved question related to the current task.
Also show a decision when the owner explicitly `requests` `review`.
A subsequent `review` cannot promise `delivery` in the conversation that ended.

The implementer chooses if the work runs during the live conversation or subsequently.
Keep current rules for permitted work and worker ownership.
Limit the wait during the live conversation.
If verification cannot finish in that interval, record pending work for checked continuation.

Do not increase `rounds`, `attempts`, workers, deadlines, or other budgets.

#### Save a fixed proposal before display

Make one narrow database relation between proposals and decisions.
Keep these records subject to validation, maintenance, and erasure rules.
Do not make a general inbox or a different memory system.
Give each proposal its own meaningless `D_ID`.

This ID names the proposal.
Callers must not derive meaning from its parts.

Before display, fix these fields:

- The complete change data after necessary normalization.

- Scope, `original` `evidence` IDs, and target revisions.

- The version of the owner-support rule described below.

- An originating `U_ID`, when applicable, and the proposed answer to that question.

Normalization prepares data before storage or equality checks.
The software names the normalized change data the canonical payload.
A proposed answer says if the change can answer all or part of the question.
The software names this question `disposition`.

Keep a digest for the proposal identity and all these unchanged fields.
Give the digest a label specified to this record type.
The software names this domain separation.
The digest connects the proposal ID to the same stored content.

Use `knowledge_write.redacted_body` and `redact_parts` consistently for storage, display, and application.
Normalize the proposal before its fields become fixed.
Record the complete normalized proposal before display.
Derive displayed content from that stored data and check its digest.

Show the complete content, targets, proposed answer, and owner-support rule.
A shortened display, or one without all necessary content, cannot receive correct approval.
If only a question or claim exists, prepare a complete proposal first.
If an earlier rejected proposal is unavailable, say so.

Prepare a new draft from checked `evidence`.
Do not reconstruct the same proposal from an error code.

#### Put reply instructions in the complete display

Before publication, include program-owned `owner_decision` instructions in the read-only `DISPLAY` response.
Keep the complete proposal and its disclosed owner-support rule unchanged.
The last display must contain everything the owner needs for the decision.
Do not put the reply instructions only in a different commentary message.

The initial display gives these unchanged sentences with its actual proposal ID:

- `Please approve memory D_ID.`

- `Please reject memory D_ID.`

The initial display also gives plain edit guidance.
For an edit, the agent prepares and displays a new complete proposal.
The subsequent checked replacement sentence is `Please replace memory D_OLD with memory D_NEW.`.
It names the two actual proposal IDs.

It supersedes the previous draft without approving the new draft.
The new proposal needs its own subsequent unchanged approval.

The owner types a reply in conversation.
The owner does not run these sentences as commands.
Use these instructions in the complete display:

1. Send one different message.

2. Use the displayed sentence unchanged, with capital letters, the ID, and the last period.

3. Do not add other text to that message.

A display, click, tool `call`, or agent-written statement gives no approval by itself.
The display adds no approval authority and changes no stored proposal or digest rule.

An edit makes a new proposal ID and needs a new complete display.
A checked owner edit replaces the earlier draft as the active draft.
It does not give approval to the new proposal.
The new proposal needs its own checked approval before application.

Approval must not apply to edited content, changed targets, or changed normalization.

#### Verify the original owner reply

The owner field is the part of the `original` record that contains the owner's complete message.
Connect each accepted decision to checked `original` owner `evidence` and the same proposal digest.
Check the `original` full owner field, its author, and its same proposal ID.
Use current source checks for that verification.

The `D_ID` identifies the proposal.
It does not replace proof of the owner's reply.

Use the accepted complete-sentence reply patterns above.
Check affirmative intent independently from checks that identify the owner.
Reject negated, conditional, quoted, ambiguous, or contradictory decisions.
Apply the same source and identity checks to refusal and replacement decisions.

A bare “approved” does not work in the reply path implemented in source.
An older design allowed such a reply only with different proof of its same proposal ID.
The source does not contain that optional alternative.
The agent must get the accepted complete sentence with the same ID.

Agent statements, CLI `calls`, recalled text, and unverified message wrappers cannot prove approval.
Do not use a tool response or copied owner text as checked `original` owner `evidence`.
Do not remove wrappers to make proof that the source checks cannot supply.

Keep `original` factual `support` different from subsequent `evidence` of the owner's decision.
Approval authorizes the displayed change.
It does not prove each claim correct.
Before application, check the two types of `evidence` again.

#### Add only the disclosed owner evidence

Freeze the proposal's `original` `supports` and owner-support rule version together.
The display must give this rule.
At application, add the actual checked owner-decision `E_ID` to each applicable claim's ordinary `support` list.
The owner does not have to know that future ID before the decision.

The versioned rule identifies the applicable `support` fields.
Keep the `original` ID sequence.
Append the actual ID only when the list does not have it.
Change no text, `kind`, `action`, scope, target revision, dependency, or expiry.

Keep each `original` `support` and check its `original` source.

The usual authority validator must see actual `direct_owner` `evidence` in those ordinary `support` lists.
An approval flag alone cannot bypass authority checks.
Keep the resulting application digest with the decision outcome and receipt.
If a changed rule would change the approved contract, a new proposal is necessary.

#### Report the actual state

Show these different `states` through agent commands and explanations:

| `State` | Meaning |
| --- | --- |
| `waiting_verification` | The proposal exists. Muninn cannot check at this time the claimed owner decision. |
| `approved_pending` | Muninn checked the `original` approval. The knowledge change has not committed. |
| `saved` | Muninn committed the knowledge change and its durable receipt together. |
| `rejected` | A checked refusal closed this draft without a knowledge write. |
| `blocked` | A specified safety, source, scope, target, setup, or policy refusal prevents `progress`. |

Keep metadata for replaced drafts and erased records different from these active outcomes.
The software names a replaced draft superseded.
An owner reply, queued worker, or a command that launches successfully cannot prove `saved`.

The source index can include the owner's reply subsequently.
While verification waits, do not invent `evidence` or tell the owner to index it.
No background worker may apply an unverified approval.

#### Recheck and save the approved change

Make a new checked application `request` with a live-conversation `origin`.
The originating unresolved `request` keeps its unchanged receipt.
Subsequent background execution keeps the checked owner-decision `origin`.

At application, check again all these conditions:

- `Original` factual `supports` and owner proof.

- Current targets and scope.

- Tombstones, policy, permitted work, and owner pauses.

- `Security` bindings and the active work generation.

A tombstone prevents erased content from coming back.
`Security` bindings connect work to its approved host, policy, and launch input.
The generation check compares the attempt's generation number with the current number.
If the numbers differ, block the record.

Approval cannot restart paused learning or widen authority or scope.
A changed target blocks application.
When needed, prepare a new proposal for display and approval.

Hold at maximum one outer database write operation.
The software names this the writer root.
Do not get its shared write lock again while it holds that lock.
While the lock is held, make no model `call`.

In one transaction, commit these records together:

- Knowledge changes.

- The receipt.

- The applied decision outcome.

- A correct resolution of an unresolved question, when applicable.

A duplicate application reports the current receipt.
An error or crash before commit does not record part of a knowledge write or an incorrect recorded `state`.

#### Handle later decisions and concurrent application

Give decision `state` a revision that only increases.
Increase it for each accepted approval, refusal, edit, or replacement of a draft.
Keep earlier accepted decision `evidence`.
A subsequent negative decision must not overwrite earlier approval proof.

In one `BEGIN IMMEDIATE` application transaction, check again these values:

- Proposal digest.

- Active approval proof.

- Decision revision.

- `approved_pending` `state`.

Use a conditional `state` update in that transaction with the knowledge and receipt writes.
If checked refusal or draft replacement commits first, prevent the previous application.
If a check or conditional update is unsuccessful, roll back all writes in that application transaction.
This rollback includes knowledge, receipt, applied outcome, and question resolution.

Keep previously committed decision `evidence`.

If application commits first, report its receipt.
A subsequent owner change needs a different checked retraction or replacement of recorded knowledge.
It cannot make the earlier recorded result unsaved.

Late proof cannot reopen rejected, superseded, or erased proposals.
A subsequent approval needs a new proposal and display.
Duplicate approval or refusal `delivery` reports the current actual `state` or receipt.
An unverified negative reply stays `waiting_verification`.

It does not prove cancellation.

#### Settle questions only after a checked save

Checks alone give no approval, permitted `evidence`, knowledge change, or question resolution.
Resolve a question only through a checked application.
The displayed proposed answer must identify and answer that question.
It must account for each affected revision that stays current.

If the change answers only part, keep the unresolved `state` and tell the remainder.
Refusal of a draft does not put back withheld knowledge or necessarily answer its question.
A replacement can make a previous affected revision stale while its historical question stays open.
Keep historical `state` different from current relevance.

#### Retain and erase all related records

Keep normalized proposals and accepted decision links until applicable erasure rules remove them.
Connect each decision to its proposal digest and `original` owner `evidence`.
Also connect it to its application `request` and `final` receipt, if available.
Do not rewrite decisions or receipts to claim approval of different content.

Keep original-support links different from owner-proof links for validation and erasure.
Prevent duplicate proof text in diagnostics or pending-work records.

Session, `event`, text-match, and transitive erasure must include each new relation.
This includes proposals, decisions, pending work, and dependent knowledge.
Transitive erasure follows links to content that depends on erased `evidence`.
If source or owner-proof `evidence` is erased, invalidate dependent content through those links.

Remove erased proposal and proof bodies from display records, pending work, and copies governed by live erasure.
Do not reconstruct erased bodies from receipts, `evidence`, diagnostics, or kept archives.
Safe kept metadata must prevent erased content from showing or applying again.
Keep provider transcripts unchanged.

Do not claim that live erasure also removed kept maintenance archives.

#### Upgrade storage without loss of safeguards

Include each new database structure in the two applicable schema fingerprint paths.
A schema fingerprint checks the tables against a known accepted structure.
Copy and rebuild must keep decision data and erasure links or safely refuse the store.
Include these relations in installer maintenance pauses, snapshots, and rollback validation.

Older tools must not silently omit decision data.

Before upgrade, the installer can read a database in known format 1.
Check its format marker, same table definitions, record links, data integrity, and recorded policy identity.
Keep current file, lock, sidecar, and process-containment checks.
Sidecars are related database files external to the primary database file.

Keep the recorded pause, project scope, and time boundary.
Do not report “no saved selection” for a format-1 read error.

A completed `process` witness alone cannot prove an earlier erasure finished.
The witness records that the `process` stopped.
If related rows or work files need cleanup, refuse before archive or candidate-store creation.
Give a fixed recovery instruction for the previous software.

A fully cleaned, checked format-1 store may continue.

Before writing format 2, take a raw format-1 snapshot.
Keep the legacy procedure for unmarked format-3 stores different from the marked format-1 procedure.
Usual runtime stays format-2-only.
The actual previous software refuses reads and writes on a populated format-2 store.

Do not promise compatibility with previous code or a downgrade at the current path.

Keep [ADR 0011](adr/0011-one-way-schema-migration-with-snapshot.md) snapshot protection.
Ship the schema, copier, fingerprints, erasure links, and rollback tests in the same unit.

Keep automatic diagnostics to constant codes, counts, and IDs.
Show proposal and question content only during explicit related `review`.
Keep that content out of logs, doctor output, and automatic startup notices.


## Hermes behaviors to use carefully

After an initial capacity error, Hermes reports current entries and specified correction instructions.
After four consolidation errors with no success between them, repair ends for that turn.
Success resets the error counter and reports a completed write.
This allows useful feedback and finite work.

It does not prove that all memory errors resolve without a person.

Hermes fixes approval replay to the same entry and refuses stale targets.
Its unattended `review` tries to record pending patch/remove proposals independently of the optional write-approval setting.
The explicit `/refine` command marks a `review` as attended.
Its background-review `origin` stays unchanged.

Muninn has policy, authority, and unchanged revision checks.
Use again those checks and add the missing connection between the same proposal and the checked decision.

Hermes `stage_write` catches a disk-write error and reports a record.
A caller can then report a pending proposal that Hermes did not record.
Do not copy this behavior.
Report recorded or complete only after durable confirmation.

Hermes summary extraction skips inherited tool results.
It does not show proposals selected for approval as applied changes.
A print operation or callback does not prove durable notice `delivery`.
The inspected auxiliary-failure warning path does not itself suppress the same errors.

Muninn should classify recorded errors to decrease duplicate reminders.
These source checks do not prove that Hermes prevents duplicate notices.

Upstream sources, pinned to the inspected commit:

- [`Store` limits, repair responses, locking, and atomic writes](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/memory_tool_store.py).

- [Tool operations, unattended deletion gate, and pinned approval replay](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/memory_tool.py).

- [Approval decisions and best-effort pending persistence](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/write_approval.py).

- [`Review` budgets, dispatch whitelist, and notification extraction](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/background_review.py).

- [Prompt refresh after compression](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/system_prompt.py#L816-L834).

- [Facts versus procedures and short-lived history](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/prompt_builder.py#L195-L232).

- [Native session search in steps](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/session_search_tool.py).

- [External provider contract](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/memory_provider.py) and [provider lifecycle manager](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/memory_manager.py).

- [RetainDB integration](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/plugins/memory/retaindb/__init__.py) and [ByteRover integration](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/plugins/memory/byterover/__init__.py).

## Other architecture options considered

### Copy curated files and hard character limits

Not selected.
Muninn has `governed` notes, `guides`, scope, and bounded `delivery`.
A small prompt block cannot replace `evidence` or justify deletion of knowledge with checked `support`.

### Add meaning-based search or an external provider

Defer this change until measured unsuccessful searches justify it.
Hermes external providers can receive queries and transcript messages.
Provider boundaries and deletion behavior differ.
Its provider manager can wait eight seconds for previously specified data.

Its built-in removal does not remove all sensitive outbound content.

The bundled RetainDB remote mirror does not propagate local deletions.
Do not generalize this to each provider.
That architecture makes a different privacy, erasure, and operating design necessary.

### Add progressive history reads

Muninn has `search`, `open`, and `session` commands for successive information.
Hermes also has content and result-count limits.
A scroll result does not prove a universal output-size cap.
These source checks do not justify a replacement search system.

### Trust all recalled text as authority

Not selected.
Recalled context gives no new owner authority.
Keep Muninn's `evidence` types, claims with checked `support`, and current validation rules.

### Retry every failed proposal with new words

Not selected.
It can consume allowance and evade a check of a claim's meaning.
Use the narrow format and reduction conditions above.

### Use format correction only

This is smaller, but one unsupported candidate can block correct candidates.
The selected reduction can keep useful unchanged candidates.
It cannot accept revised claims.

### Automatically save the valid subset

Not selected.
It changes an all-or-nothing proposal without a new `final` result from the model.
A bounded reduction `round` gives that explicit result.
It also has a deliberate no-change outcome.

### Add retries or larger limits

Not selected.
Current limits bound `calls` and retries.
Use a remaining `round`.
Keep the three-attempt and four-worker maxima.

### Add a new notice queue

Current `evidence` does not justify it.
Current recorded issues and inspection paths can help select the `action`.
The narrow proposal/decision relation serves a different purpose.
It connects the unchanged content to the owner's decision.

It does not prove `delivery` of a notice.

## HyperSkill reference and limits

HyperSkill extracts procedures after tasks that succeed.
It collects lessons from mistakes after unsuccessful tasks.
Its prompts ask what to do, when to do it, and why.
These are useful questions for a `guide` `review`.
They do not prove that `independent` `evidence` supplies `support` for a generated procedure.

The parser checks a skill name but can accept empty content.

Muninn's `GuideBody` and `NoteBody` hold content with checked `support`, conditions, `reason` values, steps, and pitfalls.
Pitfalls and decisions with checked `support` include lessons from mistakes.
Muninn keeps links to `original` sources.
Retrieved-memory feedback alone gives weaker `support`.

These source checks do not justify one more runtime feature for procedures.

HyperSkill's graph records which memories belong to the same episode.
Its links have no direction.
They do not report prerequisites, dependencies, or a skill hierarchy.
`Search` combines numerical task/plan representations with a rank based on which items appear together.

Whole-task success gives credit to all retrieved nodes and their linked episodes.
It does not prove which procedure the agent used.
Do not use this credit as proof of usefulness or stronger `evidence`.

HyperSkill's merge and removal paths overwrite or remove memory without Muninn's revision history.
Subsequent extraction can include recalled guidance again without an `independent` check for advice used again.
A missing judge result becomes `is_correct=False`.
That value can start extraction intended for unsuccessful tasks.

These behaviors justify Muninn's different `evidence` `origins` and checked learning rules.

The inspected checkout has no tracked test suite.
Its online `evaluation` changes memory by default.
The `disable_memory_evolution` and `storage_prefix` settings are available.
For future experiments, test search against a fixed store independently from changes to the store.

Do not claim improved search when the memory store silently changes between the runs being compared.

This design proposes no graph, embedding provider, merge policy, or automatic increase in trust.
An embedding provider converts text into numbers for similarity search.
Muninn distinguishes exposure to recalled advice from `independent` `evidence` used to compare results.
It also checks the agent's own report independently.

Muninn's offline replay and learning `evaluation` have `independent` labels and different test families.
They also have unchanged tests withheld from design work.
The current procedural-paraphrase fixture includes literal, reworded, and omitted exposure to advice.
Do not report these controls as missing.

Add tests for a procedure that depends on condition A for success.
Use these five conditions in the current test framework:

1. Measured success with condition A.

2. A paraphrase that keeps A.

3. A related task without A.

4. An unsuccessful run with an unknown cause.

5. Duplicate recalled advice without `independent` `evidence`.

Check that recalled advice alone does not become `independent` `support`.
Check that an unknown error cause does not become a proved pitfall.

This adds test coverage.
It does not prove a runtime defect or measured benefit.
Keep the current terse extraction prompt unless controlled `evaluation` finds a repeatable quality problem.

Muninn sources used to compare: [content models](../muninn/knowledge_models.py), [recall `review`](../muninn/cli_recall_review.py), [offline retrieval replay](../tools/replay_retrieval.py), [learning `evaluation`](../tools/learning_eval/pilot.py), and [procedural-paraphrase fixture](../tests/fixtures/learning/procedural-paraphrase.json).

HyperSkill sources, pinned to the inspected checkout:

- [Task outcome extraction prompts](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/prompts.py#L101-L189) and [extraction parser](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/provider.py#L478-L544).

- [`Graph` membership](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/hypergraph_core.py#L198-L205), [task-level credit](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/hypergraph_core.py#L295-L365), and [merge/prune behavior](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/hypergraph_core.py#L698-L950).

- [Agent recall/extraction path](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/FlashOAgents/agents.py#L199-L220) and [online `evaluation` path](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/runners/run_gaia.py#L298-L336).

## Constraints and compatibility

The accepted [ADR 0017](adr/0017-learning-scope-and-recovery.md) rejects a result with an unchanged invalid condition one time.
The owner approved the [narrow change](adr/0017-learning-scope-and-recovery.md#proposed-amendment-bounded-correction-and-action-routing) on 2026-10-29 before implementation.
It includes only the specified format corrections and removal of unchanged candidates.
All other fixed refusals end that result.

The approved maximum stays two `rounds`, three total `attempts`, and four workers.
Keep the current 5-second and 30-second waits for temporary errors.

The [approved ADR 0018](adr/0018-conversational-memory-decisions.md) includes same proposal retention and checked owner decisions.
It also includes the disclosed owner-support rule, actual question resolution, and schema maintenance.
It makes no different authority override.
Recalled text and CLI execution cannot become owner `evidence`.

Keep these checks and limits:

- Policy bindings and runner proof.

- Child-process containment and owner pauses.

- Generation checks and fixed permission to run the `request`.

- Original-source verification, scope, and authority.

- Tombstones and all-or-nothing database writes.

Do not change provider transcripts or examine private archives for this work.
Do not add provider tools, arbitrary filesystem access, or shell access.
Do not let the model control executable feedback.
Do not weaken native-event rules to correct malformed runner output.

When command answer fields change, update `docs/REFERENCE.md` and the two command-skill mirrors together.
New diagnostics stay JSON on stdout.
Provider hooks keep compact JSON and exit zero.
This design makes no claim of universal author identification, zero errors, or live cross-platform verification.

## Delivery units

1. **Usual error handling:** Implement the narrow ADR 0017 change.
   Include bounded correction, startup-only `action` selection, and safe phase diagnostics.
   Include the added HyperSkill-inspired test family.
   Do not claim a runtime improvement from test coverage alone.

   Use current tables.
   The new proposal/decision relation is not necessary for this unit.

2. **Checks and decision end:** Ship question checks and the complete owner-decision path together.
   Include unchanged proposals, complete displays, checked owner proof, subsequent checked application, receipts, and correct question resolution.
   Include schema fingerprints, copy/rebuild, installation rollback, and complete erasure links in the same checked unit.
   A view-only feature does not complete the owner's approval, edit, and refusal `request`.

Each unit needs focused checks and a last full gate.
Do not divide the decision schema from its maintenance or erasure duties.

## Acceptance tests

These checks are necessary.
They do not claim that all tests are complete without errors.

1. An accepted first-round format error receives constant feedback and can complete in `round` two.
   The same error in the last `round` ends the `attempt` without one more `call`.
   Unknown operations and invalid schema structures qualify only after native and strict JSON decoding pass.
   Muninn rejects a correction that specifies one more controlled read.

   Read and commit safety refusals do not become format retries.

2. Correction keeps `request` identity, `attempt` identity, deadline, scope, permit accounting, and original-read limits.
   Two `rounds`, three `attempts`, and four workers stay enforced, also during concurrent work.

3. Reduction can remove an invalid candidate.
   It must keep each remaining normalized candidate unchanged and in sequence.
   Muninn rejects changes to content, `kind`, `action`, target, revision, scope, `support`, expiry, or dependencies.
   An unsupported preference cannot become a fact through reduction.

4. Empty reduction makes the current no-change outcome.
   No part of a knowledge change commits before a correct terminal commit.

5. Changed `originals`, erased `evidence`, invalid authority, widened scope, and stale revisions keep current enforcement.
   A deferred refusal cannot authorize a subsequent commit after `state` changes.
   Unknown `evidence` and invalid target `state` stay terminal.
   They give no permission for reduction.

6. Native tool `events`, uncertain `processes`, and invalid `security` bindings do not start correction.
   Input/output limits and unchanged constant read limits also do not start correction.

7. Exhausted ordinary errors stay available in doctor, `request` checks, and explanations.
   Duplicate SessionStart hooks do not make dismissal necessary of these contained errors.

8. Sign-in, `security`, approval, owner-pause, uncertain-process, and unknown action-required errors stay readable.
   A newer ordinary error cannot hide a shared setup error.

9. New diagnostics give the measured phase and fixed code without transcript/model text or raw `exceptions`.
   Previous records keep `not recorded` where no information was recorded.
   Correction markers survive usual end.
   Missing markers and two-call read sequences must not make invented correction history.

10. The unresolved list uses the originating request's scope and includes applicable records with empty affected lists.
    An explicit `U_ID` shows that historical record without new `evidence` permission or assurance of current `support`.
    Erased or null bodies stay missing.
    Malformed records do not show raw content.

    After checked replacement, the previous affected revision becomes stale while the historical question `state` stays unchanged.
    A question read makes no approval, `evidence`, or knowledge change.

11. Add the five procedure conditions described above to current `evaluation` fixtures.
    Keep `independent` labels, different families, and unchanged withheld tests.
    Report added coverage until measurements prove a runtime quality problem or benefit.

12. Ordinary safe learning commits without a new approval requirement.
    A usual rejected proposal does not automatically make an owner decision.
    Related actual questions and explicit owner `review` can make complete fixed proposals for display.
    A question without a complete change needs a new displayed draft.

    So does an unavailable historical rejected proposal.

13. Displayed content comes from the stored normalized proposal and checked digest.
    An edit makes a new identity and needs new approval after its complete display.
    Previous approval cannot apply to the edited proposal.
    Unknown replies, agent statements, CLI `calls`, and unverified wrappers cannot prove a decision.

    Checked `original` complete-sentence instructions identify the same proposal.
    The owner need not type JSON, commands, or hashes.

14. Owner proof and `original` factual `support` stay different and each receive new checks.
    The fixed proposal digest includes its disclosed owner-support rule version.
    Application adds only the actual owner `E_ID` as specified.
    `Original` IDs keep their sequence and stay unchanged.

    Each other change field stays unchanged.
    The resulting digest stays with the decision outcome and receipt.

    Usual authority checks and erasure links see the actual owner `evidence` in ordinary `support` IDs.
    An approval flag alone cannot replace those `references`.
    A source-index wait makes bounded `waiting_verification` without fabricated proof or an incorrect recorded response.
    No subsequent worker can apply before checked approval.

    Pauses and changed sources, scope, policy, or current targets block safely.

15. Checked application records knowledge, receipt, decision outcome, and correct question resolution together or records none.
    Duplicate application reports the current receipt without duplicate changes.
    Error or crash must not record part of a knowledge change, an incorrect recorded status, or an incorrectly resolved question.
    Draft refusal leaves previous withheld knowledge untouched.

    An answer to only part of the question or a stale replacement alone leaves the question open.

16. Session, `event`, text-match, and transitive erasure reach all new proposals, decisions, pending work, and knowledge links.
    Source or owner-proof erasure invalidates dependent knowledge.
    Receipts and diagnostics cannot reconstruct erased bodies.
    Copy, rebuild, snapshot, rollback, and the two fingerprint paths keep the schema or safely refuse it.

    Live erasure makes no claim about deletion from kept maintenance archives.

17. Test the two sequences of concurrent decision and application with one writer root.
    Checked refusal or draft replacement committed first prevents previous application.
    Application committed first reports its current receipt.
    Late proof cannot reopen rejected, superseded, or erased proposals.

    Duplicate decisions report the current actual `state` or receipt.

    An unverified negative reply reports `waiting_verification` without an incorrect cancellation claim.
    Unsuccessful conditional updates roll back knowledge, receipt, applied outcome, and resolution together.
    Prior committed decision `evidence` stays unchanged.
    A subsequent change to recorded knowledge needs a different checked retraction or replacement.

18. Intent checks reject negated, conditional, quoted, ambiguous, and contradictory statements independently from owner identification.
    The current path needs a checked accepted complete-sentence reply with the same ID.
    A bare “approved” does not work in that path.

19. Proposal creation, display, and application use the same normalization and disclose the owner-support rule.
    A display without all necessary content cannot receive correct approval.
    Changed normalization needs a new proposal.
    The read-only `DISPLAY` response includes program-owned `owner_decision` instructions alongside the unchanged complete proposal and `support` rule.

    It gives the unchanged approve and reject sentences with its actual proposal ID, plus plain edit guidance.
    A subsequent checked replacement sentence names the previous and new IDs without approving the new proposal.
    A commentary question, click, or tool `call` cannot replace checked owner `evidence`.

20. The affected tests pass in a temporary `MUNINN_HOME`.
    Include protocol, commit, recovery, hooks, question views, decisions, erasure, maintenance, `evaluation`, and concurrent work.
    After the last edit, the last tree passes `.venv/bin/python3.13 -m tools.check --full`.
    Report live host and cross-machine verification independently.

21. Upgrade accepts only checked known format-1 stores on its format-1 path.
    It checks the marker, same table definitions, record links, integrity, policy identity, files, locks, sidecars, and `process` containment.
    It keeps the recorded pause, scope, and time boundary.
    A completed `process` witness alone does not prove an earlier erasure finished.

    Remaining rows or work files cause refusal before archive or candidate-store creation.
    The refusal gives a fixed old-program recovery instruction.

    A fully cleaned, checked format-1 store can continue.
    Upgrade takes a raw format-1 snapshot before it writes format 2.
    A format-1 read error must not become “no saved selection”.
    The unmarked format-3 path stays different.

22. Usual runtime accepts format 2 only.
    The actual previous software refuses reads and writes on a populated format-2 store.
    Provider transcripts stay unchanged.
    Kept archives cannot put back erased content into the active store.

## Revised approval recorded

The owner approved this revised behavior plan and the two ADR changes on 2026-10-29.
The authorizing statement was “all above is approved what questions would you ask”.
Implementation plans and `independent` `review` follow this approval.
The owner delegated work during the live conversation versus subsequent background execution.

That schedule is not an unresolved design selection.
Exact-proposal approval, checked `original` replies, and truthful receipt status apply in each schedule.

This approval gives project authority.
It does not prove an owner's decision about a runtime memory proposal.
