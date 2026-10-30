# Improve how Muninn checks and saves memory

Status: APPROVED by the owner on 2026-10-29.
Implemented in the private source tree.
Fresh review and the final full gate remain pending.
Publication, installation, and live verification are separate delivery checks.
Owner authorization: “all above is approved what questions would you ask”.
This approval gives authority to implement the design.
It does not prove an owner's decision about any runtime memory proposal.

The owner requested approval, edit, and reject actions in conversation.
The owner delegated the choice of work schedule to the implementer.
The implementer is the person or agent that builds this feature.
The approval includes this revised design and both ADR changes.

## How it will work

1. A session ends.
   Muninn can then review its records without an active conversation.
2. Muninn prepares proposed facts or instructions and checks their evidence.
   Ordinary safe learning saves automatically.
3. Sometimes the evidence leaves a real question that Muninn cannot safely answer.
   The agent presents it only when it matters to the current task or the owner requests review.
4. The agent prepares a complete proposed memory change.
   It shows the proposed content, any record it would replace, and the question it would answer.
   That same display gives the exact reply sentences for this proposal.
5. The owner replies in conversation.
   The agent verifies the original reply and runs the commands.
6. Muninn checks the change again before it saves anything.
   The agent reports saved, pending, rejected, or blocked from the actual result.

The owner does not need to use commands, database queries, JSON, or hashes.
A view gives no approval.
An edit creates a new proposal that needs a new display and its own approval.
A routine failed proposal does not automatically become an owner question.

This design also permits one narrow correction within the job's existing call allowance.
Muninn can correct a format error or remove specified unsupported items.
It cannot rewrite a rejected preference as a fact to evade a safety check.
If safe correction fails, other safe jobs can continue.

## What is complete

The accepted ADR 0017 decision remains in force.
This revision changes its explanation, not its authority or date.

Implementation baseline: Muninn `a5ff0733a5ba5ec37febacd5445e4c10fc59b684`.
That baseline includes the reviewed PR #76 context fix.
Initial research used `56d1977`.
The gap descriptions below describe that inspected baseline.
They do not claim that later source work is missing.

Unit 1 covers sections A–C and the added procedure tests.
It is implemented in source.
Unit 1 was merged in PR #77 at `5fa95b51e5628ca52082da846d2746473cbc961b`.
Installation and live verification remain separate delivery checks.
Section D and ADR 0018 form unit 2.
That unit is implemented in the private source tree.
Its commands connect exact complete displays and original owner replies to checked application and receipts.
Maintenance and erasure include its governed records.
Fresh review and the final full gate remain pending. Unit 2 is not published or installed by this source work.
A source change alone does not establish a release or an installed result.

The Hermes reference is commit `fcc26fbd9f1641755e49793d7ed608f3b7214c3d`, inspected on 29 October 2026.
The HyperSkill reference is a clean checkout at `a34978aa7439d2eff7ccc3d2ad5372b34d285bd5`.
The comparisons describe those source versions.
They do not establish live behavior on either system.

## Proposed changes

1. Permit one safe correction call when an existing review round remains.
   Keep two rounds, three attempts, four workers, and all evidence checks.
2. Permit format correction or removal of candidates only after specified failures.
   Keep every remaining candidate exactly as Muninn originally normalized it.
3. Stop repeated SessionStart notices for request failures that Muninn safely contained.
   Keep complete diagnostics and history.
   Keep startup notices for real owner action.
4. Record fixed failure phases and correction outcomes in existing tables.
   These changes require no new table, provider, store, or daemon.
5. Show relevant unresolved questions and complete memory proposals in conversation.
   Bind approval to the exact displayed proposal.
   Permit later checked application in the background.
6. Add procedure tests within Muninn's existing evaluation framework.
   Test success under condition A, a paraphrase that keeps A, and a related task without A.
   Test a failed run with an unknown cause and repeated recalled advice.
   Recalled advice alone must not become independent evidence.
   An unknown failure cause must not become a proved pitfall.

The procedure tests add coverage.
They do not establish a runtime defect or measured benefit.

## Purpose

Reduce avoidable failures and repeated owner work when Muninn checks memory proposals.
Keep original-source checks, author checks, scope, authority, and erasure rules in force.
Use Hermes and HyperSkill as references for demonstrated gaps.
Add only the needed changes.

Muninn should correct safe errors within its existing allowance.
If correction fails, the owner can inspect the result.
Ordinary failure notices do not require repeated dismissal.
Failures that require account access, security action, or approval must remain visible.

## Terms used in this proposal

- **Proposal:** A change that Muninn has not yet accepted as saved knowledge.
  An owner proposal is a complete memory change for the owner to review.
  The software calls it a candidate.
- **Evidence:** An original conversation record that supports a statement.
- **Job:** A request to review specified evidence or apply a checked change within fixed limits.
- **Unresolved question:** A saved question that Muninn cannot safely settle from its current evidence.
- **Candidate:** One proposed change or unresolved item in a model's final result.
  A correction can remove a candidate only under the rules below.
- **Round:** One model call within an attempt to complete a job.
- **Support reference:** The ID of evidence that supports a claim.
- **Normalized change data:** The change data after Muninn's required preparation for storage or comparison.
  The software calls this the canonical payload.
- **Owner proof:** An original owner statement whose source and author Muninn can verify.
- **Digest:** A hash that connects an identity to exact stored data.
  A label specific to the record type keeps different record types distinct.
- **Receipt:** The durable record that confirms which checked knowledge changes committed.
- **Transaction:** A database operation that commits all its changes together or rolls them all back.
- **Scope:** The global or project boundary for a request or knowledge item.
  A project scope can also have a task limit.
- **Target:** A saved record and revision that a proposal would replace or affect.
- **Revision:** A numbered version of a record.
- **Generation check:** Muninn saves a current work version number, called a generation.
  Each review attempt keeps the generation number it started with.
  When the current number changes, older attempts cannot save.
  The generation check compares the attempt's number with the current number.
  The software also calls this a generation fence.
- **Writer root:** The outer operation that holds the shared lock for database writes.
  It cannot acquire that same lock again while it holds it.
- **Owner-support rule:** The disclosed rule that adds verified owner-decision evidence to original support references.
  The software calls this support enrichment.

For example, one project record says Python 3.12.
A later record says Python 3.13.
If the evidence cannot settle the difference, Muninn keeps a question.
The agent can prepare a complete proposal to use Python 3.13 for that project.
That proposal still needs the normal evidence and owner checks.

Global scope is selected explicitly.
Ordinary project reads do not inherit global guidance.
A project scope applies to the specified project.
An optional task limit can restrict it further.

The identifiers below name records:

| Identifier | Record |
| --- | --- |
| `D_ID` | A complete proposal for an owner decision. |
| `E_ID` | An evidence record. |
| `R_ID` | A learning request or application job. |
| `U_ID` | An unresolved question. |

These are placeholders for actual IDs.
The agent gives the owner reply sentences with the actual proposal ID.

**Headless learning** runs without an active owner conversation.
**Foreground work** starts in a live owner conversation.
Background execution can finish it later.

**Governed data** remains subject to Muninn's validation, maintenance, and erasure rules.
**Admission** means permission to create or run a job under the current policy.
**Security bindings** connect work to its approved host, policy, and launch input.
A **tombstone** prevents erased content from returning.

## Existing lifecycle and reference patterns

### Read conversation records

Muninn already reads provider records without changing them.
It already supports transcript search, event reads, and session views.
Hermes also supports search followed by more detailed reads.
Keep Muninn's existing paths.
Do not add another archive, vector index, or search daemon.

### Select knowledge to retain

Muninn notes hold supported facts, preferences, decisions, and other kinds of knowledge.
Guides hold procedures and lessons.
Hermes puts user facts in `USER.md` and environment facts in `MEMORY.md`.
Task procedures belong in skills.
Keep Muninn's note and guide model.
Do not add duplicate user or environment files.

### Create a review job

SessionEnd, PreCompact, reconciliation, and checked history selection already create bounded jobs.
History confirmation binds the original bytes.
Hermes reviews conversation snapshots after a response.
Its explicit `/refine` review differs from automatic review.
Keep Muninn's existing triggers and fixed request identity.
Do not add another review worker.

### Prepare, check, and save a proposal

Muninn permits controlled evidence reads and final results within a fixed number of rounds.
At the inspected baseline, an invalid result ends the attempt.
Hermes can return correction information after memory-tool failures.
Add only the narrow correction below, within an existing remaining round.

Before save, Muninn checks original bytes, permitted evidence, authority, scope, current revisions, and tombstones.
It saves valid changes in one transaction.
Hermes checks final batch size and fixes approval to a selected entry.
Keep all Muninn checks inside its save path.
Shared candidate checks must not bypass the transaction.

### Supply context to later work

Muninn startup includes complete eligible notes and a compact guide index.
Prompt recall selects scoped context within byte limits.
Hermes uses small memory blocks and fixed prompt snapshots.
It refreshes those snapshots after context compression.
Keep Muninn's bounded, scoped delivery.
Change prompt snapshots only after a measured freshness or cache problem.

### Maintain and inspect knowledge

Muninn already handles revisions, conflicts, questions, expiry, dependencies, tombstones, and erasure.
Hermes supports memory addition, changes, and removal.
Its external providers have separate lifecycle hooks.
Keep Muninn's existing maintenance rules and default automatic learning.
Add the missing path for relevant owner decisions.

At the inspected baseline, Muninn read commands show unresolved IDs or counts without question content.
Hermes has commands to inspect pending proposals.
Add scoped inspection and complete conversational decisions without new owner authority.

### Explain failures

Muninn records safe codes for `learn show`, explanations, and doctor.
Startup selects a saved issue.
Hermes distinguishes selected pending proposals from successful writes in review notices.
Show startup action only when needed.
Keep ordinary safely contained failures available for inspection.

Muninn source anchors:
[knowledge models](../muninn/knowledge_models.py),
[bounded startup context](../muninn/knowledge_push.py),
[scoped recall](../muninn/hook_recall.py),
[capture](../muninn/hook_learn.py),
[history admission](../muninn/review_history.py),
[original evidence](../muninn/review_evidence.py),
[atomic commit](../muninn/review_commit.py), and
[erasure](../muninn/knowledge_erase.py).

These are comparable functions.
Their policies, coverage, and live verification can differ.

## Gaps confirmed in the inspected source

### 1. Safe correction stops too early

`review_supervisor_result.execute` sends model parsing errors directly to the failure callback.
It also maps native output failures to a broad rejection code.
The existing round mechanism continues after a checked read, but cannot return safe validation feedback for correction.

Permit only the correction cases defined below.
Do not let a model repeatedly rewrite a rejected claim until it passes.
For example, authority checks depend partly on the declared knowledge kind.
An unrestricted correction could relabel a preference as a fact after an authority rejection.
That would bypass the authority check.

Sources: [result reconciliation](../muninn/review_supervisor_result.py),
[round validation](../muninn/review_rounds.py), and
[candidate authority checks](../muninn/review_commit.py).

### 2. Safely contained failures can become repeated owner reminders

`review_recovery.notice` selects the setup issue first, then the latest saved request issue.
It does not distinguish a request that safely ended from a failure that needs owner action.
SessionStart can therefore repeat an ordinary model rejection until someone dismisses its record.
Keep the diagnostic record.
Change when SessionStart presents it.

Sources: [issue selection](../muninn/review_recovery.py) and
[SessionStart use](../muninn/hook.py).

### 3. Failure records lose the relevant phase

Result reconciliation groups parsing, controlled reads, evidence checks, and commit rejection into broad failure handling.
The supervisor callback defaults the diagnostic stage to `review`.
The broad label hides whether the failure came from model formatting, evidence checks, or storage.
Some older attempts have no detailed stage at all.

Sources: [result reconciliation](../muninn/review_supervisor_result.py),
[supervisor failure recording](../muninn/review_supervisor.py), and
[historical detail fallback](../muninn/review_recovery.py).

### 4. Stored unresolved questions cannot be read through ordinary commands

`save_unresolved` stores the question, disputed claim, affected revisions, and support references.
Startup supplies unresolved IDs.
Request views expose receipt IDs or counts.
At the inspected baseline, the knowledge parser and handler have no `U_ID` read path.
The owner must be able to read a withheld decision without direct database access.

The existing `unresolved_ids(scope)` query is deliberately narrower than a general question list.
It finds open questions that affect current applicable revisions.
It omits allowed questions with no affected revisions.
A new inspection path must check the originating request's scope.
It must not use the narrower query as its authorization boundary.

Inspection alone does not complete the requested approval, edit, or rejection of memory in this conversation.
The conversational decision rules below fill that gap while ordinary learning remains automatic.

Sources: [question persistence](../muninn/knowledge_write.py),
[current relevance query](../muninn/knowledge_read.py),
[knowledge parser](../muninn/cli_parser.py), and
[knowledge handler](../muninn/cli_know.py).

## Proposed behavior

### A. Bounded proposal correction

Use the request's existing `max_rounds` allowance.
The default remains two rounds.
Do not create another attempt or extend the deadline.
Do not increase worker count or change request scope.
If no round remains, record the safely contained failure.
Continue unrelated requests.

The model runner is the program that starts the model and returns its response.
The native decoder reads that response format.
The JSON decoder reads the structured data inside the response.
The schema validator checks the permitted operation and its required data fields.

The supervisor is Muninn's program that controls the review job.
A correction child is a new model process that the supervisor starts for a correction.

There are two distinct correction cases:

1. **Format correction:** Native decoding and strict JSON decoding pass.
   Local union or schema validation then rejects the structure.
   These validators check the permitted operation and required data fields.
   Until validation succeeds, do not trust `op` or call the data a final result.
   Unknown operations and invalid schema structures qualify for fixed program-owned feedback.

   The next operation must return a valid final result in the remaining last round.
   Native framing failures, invalid payload JSON, native tool events, oversized output, and runner failures do not qualify.
2. **Proposal reduction:** A fully typed final result fails one of the permitted candidate checks below.
   A fully typed result has passed schema validation.
   The corrected result can remove candidates or return no change.
   Every remaining candidate must equal its original normalized representation.
   It cannot change text, kind, action, scope, target, revision, evidence references, dependencies, or expiry.
   It cannot introduce a new claim or unresolved item.

Reduction permits removal only.
It cannot expand the batch's meaning or authority.
Remove at least one candidate.
Keep the original order and exact normalized form of every remaining candidate.
Apply this restriction to changes and unresolved items.
For an empty result, use the fixed no-change reason.

Return a typed candidate position and a fixed failure code.
Never return arbitrary exception text.
Do not copy rejected model text into trusted instructions or logs.
Give the fresh correction child fixed instructions that permit a final result only.
Give this exact empty-final form:

```json
{"op":"final","result":{"schema_version":1,"changes":[],"unresolved":[],"no_change_reason":"No supported candidates remain after correction."}}
```

For reduction, send the fully typed normalized original batch separately as untrusted temporary prompt data.
The software calls this original batch the canonical baseline.
It is comparison material, never evidence, owner authority, or instructions.
Instructions within candidate text remain data.
They cannot change the correction rules.

Allow correction only for these candidate failures:

- Candidate scope mismatch.
- Missing direct-owner support.
- A repeated replacement target.
- Overlap between replacement targets and affected unresolved targets.

A scope mismatch permits removal of that candidate only.
It never permits access to a wider scope.
An authority failure cannot permit a different knowledge kind or new support references.
Other authority failures remain terminal for that result.
Terminal means the failure ends that result without correction.

These failures also remain terminal:

- Changed originals, erased evidence, and unavailable evidence.
- Invalid read scope and uncertain process state.
- Broken security bindings.
- Unknown evidence references and invalid targets.
- Stale target revisions, policy changes, and failed generation checks.

An evidence-read limit is not a memory-capacity error.
Do not retry it unchanged.

Use a typed `ProposalRejectedError` for exactly the permitted candidate failures in the existing validation path.
After all original-evidence checks pass, the supervisor can defer that typed failure for correction.
Add a default-off `defer_candidate_failure` option to the existing commit path.
Only the supervisor may enable it, and only when one correction round remains.
Roll back the rejected transaction without governed writes or final attempt records.
All other callers and errors retain their existing behavior.

Keep the original candidate batch in temporary `Round` state for an independent exact subset check.
Send its normalized representation only to the next correction child as untrusted data.
Do not retain raw output with an invalid schema.
Do not save the original batch in history, call markers, diagnostics, logs, or receipts.
Only independently accepted knowledge follows the normal retention rules.
After restart, block the old attempt instead of reconstructing a lost batch.

At corrected commit, repeat all current checks against saved state and original bytes.
Use no extra preflight evidence read.
Do not acquire the write lock again while the outer operation holds it.

Permit at most one correction call.
A preceding read round can consume that opportunity.
The correction must return a final operation.
It cannot request another controlled read.
Identify format correction at the decoding/schema boundary.
Identify reduction through its typed candidate error.
Never use exception text to convert a controlled-read or commit safety refusal into format feedback.

Before every correction call, refresh the active request and the complete set of evidence it permits.
Recheck policy, generation, round allowance, original-read budgets, deadline, and knowledge freshness.
Retain the same permit and attempt accounting.

The existing `max_input_bytes` cap covers the complete UTF-8 prompt.
It includes fixed instructions, feedback, and the temporary original batch.
If the complete prompt cannot fit, stop with `prompt_limit` before another reservation or launch.
Do not truncate the original batch or omit required context.
Do not increase limits or add calls.

### B. Owner-action selection

When issues are read, use a fixed code policy to classify them.
Reuse the existing issue table and inspection commands where possible.
A contained request failure remains available through `learn show` and its explanation.
It does not require dismissal to keep later SessionStart hooks quiet.

Keep these states visible at startup:

- Sign-in or account access is required.
- Muninn cannot verify an approved capability, host, launch input, or policy binding.
- A review reported a prohibited native tool event.
- Muninn cannot prove that a previous process stopped.
- Learning remains paused or requires an existing owner approval action.
- Muninn cannot safely classify an unknown failure as contained.

If new work remains safe, ordinary proposal rejection does not require owner action.
The same applies to exhausted format correction and exhausted safe retries.
A fixed source-read limit remains available for inspection with its cause and maintainer action.
It must not become a repeated request to rerun unchanged work.
Do not hide an active shared setup failure because a newer request failure exists.

Add an action selector used only at startup.
Preserve the existing `notice()` selection.
Preserve every issue that doctor reports as requiring attention.
Routine failures remain visible in doctor, request history, and explanations.
Do not report doctor as healthy merely because startup stopped a repeated notice.

Derive presentation from saved state.
This avoids a new delivery ledger, expiry policy, or dismissal queue.

### C. Safe phase diagnostics

Record one fixed phase for each new failure:

- Native output.
- Model format.
- Candidate validation.
- Controlled read.
- Original evidence.
- Commit.

If an existing fixed error code gives a more specific cause, keep it.
Do not store transcript text, model content, raw validation messages, or provider secrets in diagnostics.

Expose the phase through existing request details and explanation output.
Use `not recorded` for old attempts without evidence.
Do not infer historical phases.

Use fixed program-owned `review_call.outcome` values to mark a completed call that requested correction.
First check the completed call, current attempt, policy, and generation.
Then save the marker in one transaction.
Keep existing terminal-call state, usage, and charge accounting unchanged.

A later launched correction call proves that correction was attempted.
To prove exhaustion, also require the marker, completed correction call, and final failure.
Two calls alone do not prove correction.
The first call can request a controlled read.
If saved history cannot establish the distinction, use `unknown`.

Add no schema field or table.
Temporary `Round` data controls live subset comparison.
It cannot prove historical correction.

### D. Unresolved question inspection and conversational decisions

#### Read the saved question without changing it

Add `muninn know unresolved [U_ID] [--human]`.
Without an ID, list applicable unresolved records within a bounded page.
With an ID, show the stored question, claim, affected revisions, support references, and originating request.
Default output is JSON.
The `--human` option gives an explanation for a person.
Read operations must not change an unresolved record's content or state.

Use the originating request's `scope_json` to check list access under existing project/global rules.
Include records with an empty affected list.
Do not infer their scope from affected knowledge alone.

An explicit `U_ID` opens that exact historical record, as `know show` does for knowledge IDs.
It permits no additional evidence read and gives no assurance of current support.
For an erased record or null `item_json`, return safe metadata without a body.
Do not reconstruct erased questions from receipts, evidence, or old model output.

Apply existing redaction and output bounds.
For malformed records, fail safely without raw stored content.
Keep question and claim content out of logs, doctor, and automatic startup notices.
For an unknown ID, return a safe not-found response.

Distinguish current relevance from historical state.
A checked replacement can activate revision N+1 of a withheld target.
A question that affects only revision N no longer withholds that replacement.
The original question can remain historically open.
Mark its affected revision stale.
Do not report automatic resolution.

A view gives no approval, new evidence, authority, or knowledge change.
The question itself remains a model proposal.
The owner gives approval, requests an edit, or rejects the complete proposal in conversation.
The agent runs all necessary commands.
Do not ask the owner to inspect the database or run commands.

#### Selection and scheduling

Keep ordinary automatic learning free of a new approval requirement.
The `SessionEnd` hook can create a job for later review without an active conversation.
Do not require an owner decision for every learned item or safely contained proposal failure.

Present a decision for a genuine unresolved question relevant to the current task.
Also present a decision when the owner explicitly requests review.
A later review cannot promise delivery in the conversation that already ended.

The implementer chooses whether the work runs during the live conversation or later.
Keep existing rules for permitted work and worker ownership.
Limit the wait during the live conversation.
If verification cannot finish within that interval, save pending work for checked continuation.
Do not increase rounds, attempts, workers, deadlines, or other budgets.

#### Save a fixed proposal before display

Create one narrow database relation between proposals and decisions.
Keep these records subject to validation, maintenance, and erasure rules.
Do not create a general inbox or another memory system.
Give each proposal its own opaque `D_ID`.
This ID names the proposal.
Callers must not derive meaning from its parts.

Before display, fix these fields:

- The complete change data after required normalization.
- Scope, original evidence references, and target revisions.
- The version of the owner-support rule described below.
- Any originating `U_ID` and the proposed answer to that question.

Normalization is the required preparation of data before storage or comparison.
The software calls the normalized change data the canonical payload.
A proposed answer includes whether the change would fully or partly settle the question.
The software calls this question disposition.

Retain a digest over the proposal identity and all these fixed fields.
Give the digest a label specific to this record type.
The software calls this domain separation.
The digest connects the proposal ID to the exact stored content.

Use `knowledge_write._redacted_body` and `redact_parts` consistently for storage, display, and application.
Normalize the proposal before its fields become fixed.
Save the complete normalized proposal before display.
Derive displayed content from that stored data and verify its digest.

Show the complete content, targets, proposed answer, and owner-support rule.
A shortened or materially incomplete display cannot receive valid approval.
If only a question or claim exists, prepare a complete proposal first.
If an earlier rejected proposal is unavailable, say so.
Prepare a new draft from checked evidence.
Do not reconstruct an exact proposal from a failure code.

#### Put reply instructions in the complete display

Before publication, include program-owned `owner_decision` instructions in the read-only `DISPLAY` response.
Keep the complete proposal and its disclosed owner-support rule unchanged.
The final display must contain everything the owner needs for the decision.
Do not put the reply instructions only in a separate commentary message.

The initial display gives these exact sentences with its actual proposal ID:

- `Please approve memory D_ID.`
- `Please reject memory D_ID.`

The initial display also gives plain edit guidance.
For an edit, the agent prepares and displays a new complete proposal.
The later checked replacement sentence is `Please replace memory D_OLD with memory D_NEW.`.
It names both actual proposal IDs. It supersedes the old draft without approving the new draft.
The new proposal needs its own later exact approval.

The owner types a reply in conversation.
The owner does not run these sentences as commands.
Use these instructions in the complete display:

1. Send one separate message.
2. Use the displayed sentence exactly, including capital letters, the ID, and the final period.
3. Do not add other text to that message.

A display, click, tool call, or agent-written statement gives no approval by itself.
The display adds no approval authority and changes no stored proposal or digest rule.

An edit creates a new proposal ID and requires a fresh complete display.
A verified owner edit replaces the earlier draft as the active draft.
It does not give approval to the new proposal.
The new proposal needs its own verified approval before application.
Never transfer approval to edited content, changed targets, or changed normalization.

#### Verify the original owner reply

The owner field is the part of the original record that contains the owner's complete message.
Connect each accepted decision to verified original owner evidence and the exact proposal digest.
Verify the original whole owner field, its author, and its exact proposal reference.
Use current source checks for that verification.
The `D_ID` identifies the proposal.
It does not replace proof of the owner's reply.

Use the supported complete-sentence reply forms above.
Check affirmative intent separately from checks that identify the owner.
Reject negated, conditional, quoted, ambiguous, or conflicting decisions.
Apply the same source and identity checks to rejection and replacement decisions.

A bare “approved” does not work in the reply path implemented in source.
An older design allowed such a reply only with separate proof of its exact proposal reference.
That optional alternative is not implemented.
The agent must obtain the supported complete sentence with the exact ID.

Agent statements, CLI calls, recalled text, and unverified message wrappers cannot prove approval.
Do not treat a tool response or copied owner text as verified original owner evidence.
Do not remove wrappers to manufacture proof that the source checks cannot establish.

Keep original factual support separate from later evidence of the owner's decision.
Approval authorizes the displayed change.
It does not prove every claim true.
Before application, verify both forms of evidence again.

#### Add only the disclosed owner evidence

Fix the proposal's original supports and owner-support rule version together.
The display must explain this rule.
At application, add the real verified owner-decision `E_ID` to each applicable claim's ordinary support list.
The owner need not know that future ID before the decision.

The versioned rule identifies the applicable support fields.
Keep the original reference order.
Append the real ID only when it is absent.
Change no text, kind, action, scope, target revision, dependency, or expiry.
Keep every original support and verify its original source.

The normal authority validator must see actual `direct_owner` evidence in those ordinary support lists.
A separate approval flag cannot bypass authority checks.
Retain the resulting application digest with the decision outcome and receipt.
If a changed rule would alter the approved contract, require a fresh proposal.

#### Report the actual state

Expose these distinct states through agent commands and explanations:

| State | Meaning |
| --- | --- |
| `waiting_verification` | The proposal exists. Muninn cannot yet verify the claimed owner decision. |
| `approved_pending` | Muninn verified the original approval. The knowledge change has not committed. |
| `saved` | Muninn committed the knowledge change and its durable receipt together. |
| `rejected` | A verified rejection closed this draft without a knowledge write. |
| `blocked` | A specific safety, source, scope, target, setup, or policy refusal prevents progress. |

Keep metadata for replaced drafts and erased records distinct from these active outcomes.
The software calls a replaced draft superseded.
An owner reply, queued worker, or successful command launch cannot establish `saved`.

The source index can take time to include the owner's reply.
While verification waits, do not fabricate evidence or ask the owner to index it.
No background worker may apply an unverified approval.

#### Recheck and save the approved change

Create a new checked application request with a live-conversation origin.
The originating unresolved request keeps its fixed receipt.
Later background execution keeps the verified owner-decision origin.

At application, recheck all these conditions:

- Original factual supports and owner proof.
- Current targets and scope.
- Tombstones, policy, permitted work, and owner pauses.
- Security bindings and the active work generation.

A tombstone prevents erased content from returning.
Security bindings connect work to its approved host, policy, and launch input.
The generation check compares the attempt's generation number with the current number.
If the numbers differ, block the save.

Approval cannot restart paused learning or widen authority or scope.
A changed target blocks application.
When needed, prepare a fresh proposal for display and approval.

Hold at most one outer database write operation.
The software calls this the writer root.
Do not acquire its shared write lock again while it holds that lock.
While the lock is held, make no model call.

In one transaction, commit these records together:

- Knowledge changes.
- The receipt.
- The applied decision outcome.
- Any valid resolution of an unresolved question.

A duplicate application returns the existing receipt.
A failure or crash before commit leaves no partial knowledge or false saved state.

#### Handle later decisions and concurrent application

Give decision state a revision that only increases.
Advance it for each accepted approval, rejection, edit, or replacement of a draft.
Preserve earlier accepted decision evidence.
A later negative decision must not overwrite earlier approval proof.

Inside one `BEGIN IMMEDIATE` application transaction, recheck these values:

- Proposal digest.
- Active approval proof.
- Decision revision.
- `approved_pending` state.

Use a conditional state update in that transaction with the knowledge and receipt writes.
If verified rejection or draft replacement commits first, prevent the old application.
If any check or conditional update fails, roll back all writes in that application transaction.
This rollback includes knowledge, receipt, applied outcome, and question resolution.
Preserve previously committed decision evidence.

If application commits first, return its receipt.
A later owner change needs a separate checked retraction or replacement of saved knowledge.
It cannot make the earlier saved result unsaved.

Late proof cannot reopen rejected, superseded, or erased proposals.
A later approval requires a fresh proposal and display.
Duplicate approval or rejection delivery returns the current actual state or receipt.
An unverified negative reply remains `waiting_verification`.
It does not establish cancellation.

#### Settle questions only after a checked save

Inspection alone creates no approval, permitted evidence, knowledge change, or question resolution.
Resolve a question only through a checked application.
The displayed proposed answer must identify and answer that question.
It must account for every affected revision that remains current.

If the change answers only part, keep the unresolved state and explain the remainder.
Rejection of a draft does not restore withheld knowledge or necessarily answer its question.
A replacement can make an old affected revision stale while its historical question stays open.
Keep historical state distinct from current relevance.

#### Retain and erase all related records

Retain normalized proposals and accepted decision links until applicable erasure rules remove them.
Connect each decision to its proposal digest and original owner evidence.
Also connect it to its application request and final receipt when present.
Do not rewrite decisions or receipts to claim approval of different content.

Keep original-support links separate from owner-proof links for validation and erasure.
Avoid duplicate proof text in diagnostics or pending-work records.

Session, event, text-match, and transitive erasure must reach every new relation.
This includes proposals, decisions, pending work, and dependent knowledge.
Transitive erasure follows links to content that depends on erased evidence.
If source or owner-proof evidence is erased, invalidate dependent content through those links.

Remove erased proposal and proof bodies from display records, pending work, and copies governed by live erasure.
Do not reconstruct erased bodies from receipts, evidence, diagnostics, or retained archives.
Safe retained metadata must not permit erased content to appear or apply again.
Keep provider transcripts unchanged.
Do not claim that live erasure also removed retained maintenance archives.

#### Upgrade storage without loss of safeguards

Register every new database structure in both applicable schema fingerprint paths.
A schema fingerprint checks the tables against a known supported structure.
Copy and rebuild must preserve decision data and erasure links or safely refuse the store.
Include these relations in installer maintenance pauses, snapshots, and rollback validation.
Older tools must not silently omit decision data.

Before upgrade, the installer can read a database in known format 1.
Check its format marker, exact table definitions, record links, data integrity, and saved policy identity.
Keep existing file, lock, sidecar, and process-containment checks.
Sidecars are related database files outside the main database file.
Preserve the saved pause, project scope, and time boundary.
Do not treat a format-1 read error as “no saved selection”.

A completed process witness alone cannot prove an earlier erasure finished.
The witness records that the process stopped.
If related rows or work files still need cleanup, refuse before archive or candidate-store creation.
Give a fixed recovery instruction for the old program.
A fully cleaned, verified format-1 store may proceed.

Before writing format 2, take a raw format-1 snapshot.
Keep the separate legacy route for unmarked format-3 stores distinct.
Normal runtime remains format-2-only.
The actual old program refuses both reads and writes on a populated format-2 store.
Do not promise backward compatibility or an in-place downgrade.

Preserve [ADR 0011](adr/0011-one-way-schema-migration-with-snapshot.md) snapshot protection.
Ship the schema, copier, fingerprints, erasure links, and rollback tests in the same unit.

Keep automatic diagnostics to fixed codes, counts, and IDs.
Show proposal and question content only during explicit relevant review.
Keep that content out of logs, doctor output, and automatic startup notices.


## Hermes behaviors to use carefully

After an initial capacity failure, Hermes returns current entries and exact correction instructions.
Its fourth consecutive consolidation failure ends repair for that turn.
Success resets the failure counter and reports a completed write.
This supports useful feedback and finite work.
It does not prove that all memory failures resolve without a person.

Hermes fixes approval replay to an exact entry and refuses stale targets.
Its unattended review stages patch/remove proposals independently of the optional write-approval setting.
The explicit `/refine` command marks a review as attended.
Its background-review origin stays unchanged.
Muninn already has policy, authority, and fixed revision checks.
Reuse those checks and add the missing connection between the exact proposal and the verified decision.

Hermes `stage_write` catches a disk-write failure and still returns a record.
A caller can then report a staged proposal that Hermes did not save.
Do not copy this behavior.
Report saved or complete only after durable confirmation.

Hermes summary extraction skips inherited tool results.
It does not present selected staged proposals as applied changes.
A print operation or callback does not prove durable notice delivery.
The inspected auxiliary-failure warning path does not itself suppress identical failures.
Muninn should classify saved failures to reduce repeated reminders.
The comparison does not establish that Hermes prevents duplicate notices.

Upstream sources, pinned to the inspected commit:

- [Store bounds, repair responses, locking, and atomic writes](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/memory_tool_store.py).
- [Tool operations, unattended deletion gate, and pinned approval replay](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/memory_tool.py).
- [Approval decisions and best-effort pending persistence](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/write_approval.py).
- [Review budgets, dispatch whitelist, and notification extraction](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/background_review.py).
- [Prompt refresh after compression](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/system_prompt.py#L816-L834).
- [Facts versus procedures and short-lived history](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/prompt_builder.py#L195-L232).
- [Native progressive session search](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/session_search_tool.py).
- [External provider contract](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/memory_provider.py) and [provider lifecycle manager](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/memory_manager.py).
- [RetainDB integration](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/plugins/memory/retaindb/__init__.py) and [ByteRover integration](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/plugins/memory/byterover/__init__.py).

## Other architecture options considered

### Copy curated files and hard character limits

Not selected.
Muninn already has governed notes, guides, scope, and bounded delivery.
A small prompt block cannot replace evidence or justify deletion of supported knowledge.

### Add meaning-based search or an external provider

Defer this change until measured search failures justify it.
Hermes external providers can receive queries and transcript messages.
Provider boundaries and deletion behavior differ.
Its provider manager can wait eight seconds for data requested in advance.
Its built-in removal of sensitive outbound content has limited coverage.

The bundled RetainDB remote mirror does not propagate local deletions.
Do not generalize this to every provider.
That architecture would require a separate privacy, erasure, and operational design.

### Add progressive history reads

Muninn already supports search, open, and session commands for successive detail.
Hermes also has content and result-count limits.
A scroll result does not prove a universal output-size cap.
This comparison does not justify a replacement search system.

### Trust all recalled text as authority

Not selected.
Recalled context gives no new owner authority.
Keep Muninn's evidence types, supported claims, and current validation rules.

### Retry every failed proposal with new words

Not selected.
It can consume allowance and evade a check of a claim's meaning.
Use the narrow format and reduction cases above.

### Use format correction only

This is smaller, but one unsupported candidate can block valid candidates.
The selected reduction can retain useful unchanged candidates.
It cannot accept revised claims.

### Automatically save the valid subset

Not selected.
It changes an all-or-nothing proposal without a new final result from the model.
A bounded reduction round gives that explicit result.
It also permits a deliberate no-change outcome.

### Add retries or larger limits

Not selected.
Existing limits already bound calls and retries.
Use a remaining round.
Preserve the three-attempt and four-worker maxima.

### Add a new notice queue

Current evidence does not justify it.
Existing saved issues and inspection paths can support action selection.
The narrow proposal/decision relation serves a different purpose.
It connects the exact content to the owner's decision.
It does not prove delivery of a notice.

## HyperSkill reference and limits

HyperSkill extracts procedures after successful tasks and lessons from mistakes after failed tasks.
Its prompts ask what to do, when to do it, and why.
These are useful questions for a guide review.
They do not prove that independent evidence supports a generated procedure.
The parser checks a skill name but can accept empty content.

Muninn's `GuideBody` and `NoteBody` already hold supported content, conditions, reasons, steps, and pitfalls.
Supported pitfalls and decisions already cover lessons from mistakes.
Muninn keeps links to original sources.
Retrieved-memory feedback alone gives weaker support.
This comparison does not justify an extra runtime feature for procedures.

HyperSkill's graph records which memories belong to the same episode.
Its links have no direction.
They do not describe prerequisites, dependencies, or a skill hierarchy.
Search combines numerical task/plan representations with a rank based on which items appear together.
Whole-task success gives credit to all retrieved nodes and their linked episodes.
It does not prove which procedure the agent used.
Do not treat this credit as verified usefulness or stronger evidence.

HyperSkill's merge and removal paths overwrite or delete memory without Muninn's revision history.
Recalled guidance can enter later extraction again without an independent check for reused advice.
A missing judge result becomes `is_correct=False`.
That value can trigger extraction intended for failed tasks.
These behaviors support Muninn's separate evidence origins and checked learning rules.

The inspected checkout has no tracked test suite.
Its online evaluation changes memory by default.
It supports `disable_memory_evolution` and separate `storage_prefix` values.
For future experiments, test search against a fixed store separately from changes to the store.
Do not claim a search gain when the store silently changes between comparisons.

This design proposes no graph, embedding provider, merge policy, or automatic increase in trust.
An embedding provider converts text into numbers for similarity search.
Muninn already separates exposure to recalled advice from independent comparison evidence.
It also treats the agent's own report separately.

Muninn's offline replay and learning evaluation support independent labels and separate test families.
They also support fixed test cases withheld from design work.
The existing procedural-paraphrase fixture covers literal, reworded, and omitted exposure to advice.
Do not describe these controls as missing.

Add tests for a procedure whose success depends on condition A.
Use these five cases within the existing test framework:

1. Observed success under condition A.
2. A paraphrase that keeps A.
3. A related task without A.
4. A failed run with an unknown cause.
5. Repeated recalled advice without independent evidence.

Check that recalled advice alone does not become independent support.
Check that an unknown failure cause does not become a proved pitfall.

This adds test coverage.
It does not establish a runtime defect or measured benefit.
Keep the current terse extraction prompt unless controlled evaluation finds a repeatable quality problem.

Muninn comparison sources: [content models](../muninn/knowledge_models.py),
[recall review](../muninn/cli_recall_review.py),
[offline retrieval replay](../tools/replay_retrieval.py),
[learning evaluation](../tools/learning_eval/pilot.py), and
[procedural-paraphrase fixture](../tests/fixtures/learning/procedural-paraphrase.json).

HyperSkill sources, pinned to the inspected checkout:

- [Success/failure extraction prompts](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/prompts.py#L101-L189) and [extraction parser](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/provider.py#L478-L544).
- [Graph membership](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/hypergraph_core.py#L198-L205), [task-level credit](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/hypergraph_core.py#L295-L365), and [merge/prune behavior](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/hyperskill/hypergraph_core.py#L698-L950).
- [Agent recall/extraction path](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/FlashOAgents/agents.py#L199-L220) and [online evaluation path](https://github.com/rux001/HyperSkill/blob/a34978aa7439d2eff7ccc3d2ad5372b34d285bd5/runners/run_gaia.py#L298-L336).

## Constraints and compatibility

The accepted [ADR 0017](adr/0017-learning-scope-and-recovery.md) rejects a result with a fixed invalid condition once.
The owner approved the [narrow amendment](adr/0017-learning-scope-and-recovery.md#proposed-amendment-bounded-correction-and-action-routing)
on 2026-10-29 before implementation.
It permits only the specified format corrections and removal of unchanged candidates.
All other fixed refusals still end that result.
The approved maximum remains two rounds, three total attempts, and four workers.
Keep the existing 5-second and 30-second delays for temporary failures.

The [approved ADR 0018](adr/0018-conversational-memory-decisions.md) covers exact proposal retention and verified owner decisions.
It also covers the disclosed owner-support rule, real question resolution, and schema maintenance.
It creates no separate authority override.
Recalled text and CLI execution cannot become owner evidence.

Preserve these checks and limits:

- Policy bindings and runner proof.
- Child-process containment and owner pauses.
- Generation checks and fixed permission to run the request.
- Original-source verification, scope, and authority.
- Tombstones and all-or-nothing database writes.

Do not alter provider transcripts or inspect private archives for this work.
Do not add provider tools, arbitrary filesystem access, or shell access.
Do not let the model control executable feedback.
Do not weaken native-event rules to correct malformed runner output.

When command answer fields change, update `docs/REFERENCE.md` and both command-skill mirrors together.
New diagnostics remain JSON on stdout.
Provider hooks retain compact JSON and exit zero.
This design makes no claim of universal author identification, zero failures, or live cross-platform verification.

## Delivery units

1. **Routine failure handling:** Implement the narrow ADR 0017 amendment.
   Include bounded correction, startup-only action selection, and safe phase diagnostics.
   Include the added HyperSkill-inspired test family.
   Do not claim a runtime improvement from test coverage alone.
   Use existing tables.
   This unit does not depend on the new proposal/decision relation.
2. **Inspection and decision completion:** Ship question inspection and the complete owner-decision path together.
   Include exact proposals, complete displays, verified owner proof, later checked application, receipts, and valid question resolution.
   Include schema fingerprints, copy/rebuild, installation rollback, and complete erasure links in the same reviewed unit.
   A view-only feature does not complete the owner's approval, edit, and rejection request.

Each unit requires focused checks and a final full gate.
Do not separate the decision schema from its maintenance or erasure duties.

## Acceptance tests

These are required checks, not claims that all tests have passed.

1. A supported first-round format error receives fixed feedback and can complete in round two.
   The same failure in the final round ends the attempt without another call.
   Unknown operations and invalid schema structures qualify only after native and strict JSON decoding pass.
   Muninn rejects a correction that requests another controlled read.
   Read and commit safety refusals never become format retries.
2. Correction preserves request identity, attempt identity, deadline, scope, permit accounting, and original-read limits.
   Two rounds, three attempts, and four workers remain enforced, also during concurrent work.
3. Reduction can remove an invalid candidate.
   It must preserve every remaining normalized candidate exactly and in order.
   Muninn rejects changes to content, kind, action, target, revision, scope, support, expiry, or dependencies.
   An unsupported preference cannot become a fact through reduction.
4. Empty reduction produces the existing no-change outcome.
   No partial knowledge change occurs before a valid final commit.
5. Changed originals, erased evidence, invalid authority, widened scope, and stale revisions retain current enforcement.
   A deferred rejection cannot authorize a later commit after state changes.
   Unknown evidence and invalid target state remain terminal.
   They do not permit reduction.
6. Native tool events, uncertain processes, and invalid security bindings do not trigger correction.
   Input/output limits and unchanged fixed read limits do not trigger correction either.
7. Exhausted ordinary failures remain available in doctor, request inspection, and explanations.
   Repeated SessionStart hooks do not require dismissal of these contained failures.
8. Sign-in, security, approval, owner-pause, uncertain-process, and unknown action-required failures remain visible.
   A newer ordinary failure cannot hide a shared setup failure.
9. New diagnostics give the observed phase and fixed code without transcript/model text or raw exceptions.
   Old records retain `not recorded` where detail is absent.
   Correction markers survive normal completion.
   Missing markers and two-call read sequences must not produce invented correction history.
10. The unresolved list uses the originating request's scope and includes applicable records with empty affected lists.
    An explicit `U_ID` shows that historical record without new evidence permission or assurance of current support.
    Erased or null bodies remain absent.
    Malformed records do not expose raw content.
    After checked replacement, the old affected revision becomes stale while the historical question state stays unchanged.
    A question read creates no approval, evidence, or knowledge change.
11. Add the five procedure cases described above to existing evaluation fixtures.
    Keep independent labels, separate families, and fixed withheld test cases.
    Report added coverage until measurements establish a runtime quality problem or benefit.
12. Ordinary safe learning commits without a new approval requirement.
    A routine rejected proposal does not automatically create an owner decision.
    Relevant genuine questions and explicit owner review can produce complete fixed proposals for display.
    A question without a complete change needs a fresh displayed draft.
    So does an unavailable historical rejected proposal.
13. Displayed content comes from the stored normalized proposal and checked digest.
    An edit creates a new identity and needs fresh approval after its complete display.
    Old approval cannot apply to the edited proposal.
    Unknown replies, agent statements, CLI calls, and unverified wrappers cannot establish a decision.
    Verified original complete-sentence instructions identify the exact proposal.
    The owner need not type JSON, commands, or hashes.
14. Owner proof and original factual support stay separate and both receive fresh checks.
    The fixed proposal digest includes its disclosed owner-support rule version.
    Application adds only the real owner `E_ID` as specified.
    Original references keep their order and remain intact.
    Every other change field stays unchanged.
    The resulting digest stays with the decision outcome and receipt.

    Normal authority checks and erasure links see the actual owner evidence in ordinary support references.
    A separate approval flag cannot replace those references.
    A source-index delay produces bounded `waiting_verification` without fabricated proof or a false saved response.
    No later worker can apply before verified approval.
    Pauses and changed sources, scope, policy, or current targets block safely.
15. Checked application saves knowledge, receipt, decision outcome, and valid question resolution together or saves none.
    Duplicate application returns the existing receipt without duplicate changes.
    Failure or crash cannot leave partial knowledge, a false saved status, or a false resolved question.
    Draft rejection leaves old withheld knowledge untouched.
    A partial answer or stale replacement alone leaves the question open.
16. Session, event, text-match, and transitive erasure reach all new proposals, decisions, pending work, and knowledge links.
    Source or owner-proof erasure invalidates dependent knowledge.
    Receipts and diagnostics cannot reconstruct erased bodies.
    Copy, rebuild, snapshot, rollback, and both fingerprint paths preserve the schema or safely refuse it.
    Live erasure makes no claim about deletion from retained maintenance archives.
17. Test both orders of concurrent decision and application under one writer root.
    Verified rejection or draft replacement committed first prevents old application.
    Application committed first returns its existing receipt.
    Late proof cannot reopen rejected, superseded, or erased proposals.
    Duplicate decisions return the current actual state or receipt.

    An unverified negative reply reports `waiting_verification` without a false cancellation claim.
    Failed conditional updates roll back knowledge, receipt, applied outcome, and resolution together.
    Prior committed decision evidence remains unchanged.
    A later change to saved knowledge needs a separate checked retraction or replacement.
18. Intent checks reject negated, conditional, quoted, ambiguous, and conflicting statements separately from owner identification.
    The current path requires a verified supported complete-sentence reply with the exact ID.
    A bare “approved” does not work in that path.
19. Proposal creation, display, and application use the same normalization and disclose the owner-support rule.
    An incomplete display cannot receive valid approval.
    Changed normalization requires a fresh proposal.
    The read-only `DISPLAY` response includes program-owned `owner_decision` instructions alongside the unchanged complete proposal and support rule.
    It gives the exact approve and reject sentences with its actual proposal ID, plus plain edit guidance.
    A later checked replacement sentence names both the old and new IDs without approving the new proposal.
    A separate commentary question, click, or tool call cannot substitute for verified owner evidence.
20. Focused tests pass in a temporary `MUNINN_HOME`.
    Cover protocol, commit, recovery, hooks, question views, decisions, erasure, maintenance, evaluation, and concurrent work.
    After the last edit, the final tree passes `.venv/bin/python3.13 -m tools.check --full`.
    Report live host and cross-machine verification separately.
21. Upgrade accepts only verified known format-1 stores on its format-1 path.
    It checks the marker, exact table definitions, record links, integrity, policy identity, files, locks, sidecars, and process containment.
    It preserves the saved pause, scope, and time boundary.
    A completed process witness alone does not prove an earlier erasure finished.
    Remaining rows or work files cause refusal before archive or candidate-store creation.
    The refusal gives a fixed old-program recovery instruction.

    A fully cleaned, verified format-1 store can proceed.
    Upgrade takes a raw format-1 snapshot before it writes format 2.
    A format-1 read error must not become “no saved selection”.
    The unmarked format-3 path stays separate.
22. Normal runtime accepts format 2 only.
    The actual old program refuses both reads and writes on a populated format-2 store.
    Provider transcripts remain unchanged.
    Retained archives cannot restore erased content into the active store.

## Revised approval recorded

The owner approved this revised behavior plan and both ADR changes on 2026-10-29.
The authorizing statement was “all above is approved what questions would you ask”.
Implementation plans and independent review follow this approval.
The owner delegated work during the live conversation versus later background execution.
That schedule is not an unresolved product choice.
Exact-proposal approval, verified original replies, and truthful receipt status apply under either schedule.

This approval gives project authority.
It does not prove an owner's decision about any runtime memory proposal.
