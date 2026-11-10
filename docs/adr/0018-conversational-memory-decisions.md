# 0018: Connect owner decisions to exact memory proposals

## Status

ACCEPTED by the owner on 2026-10-29.
The source implements the commands, checked application, maintenance, and erasure.
A source change does not prove the selected release, local installation, or live verification.
Owner authorization: “all above is approved what questions would you ask”.

This approval authorizes implementation.
It does not prove an owner's decision about a runtime memory proposal.

Related proposal: [memory lifecycle design](../memory-lifecycle-design.md).
The different [ADR 0017 change](0017-learning-scope-and-recovery.md#proposed-amendment-bounded-correction-and-action-routing) includes usual correction and error notices.

## What the owner will see

After a session ends, Muninn can examine its records without an active conversation.
It checks proposed facts or instructions before it records knowledge.
Ordinary safe learning stays automatic.
Approval for each memory is not necessary.

Sometimes, the records leave an actual question that Muninn cannot safely answer.
If that question affects the current task, the agent can get an answer from the owner.
The owner can also `request` a memory `review`.
A usual unsuccessful proposal does not, by itself, need an owner decision.

The agent first prepares a complete proposed memory change.
It shows the proposed recorded content, replacement targets, and answer to a question.
The same display gives the unchanged reply sentences with that proposal's ID.
The owner replies in conversation.

The agent checks the `original` reply and runs the necessary commands.

Muninn then records the checked change or reports why it stays pending or blocked.
The agent reports “saved” only after Muninn confirms the database change and its receipt.
The owner need not run commands, examine the database, type JSON, or supply a hash.

## Terms

- **Proposal:** A complete memory change that the owner can examine before Muninn records it.
  The software also names it a candidate.

- **`Evidence`:** An `original` conversation record that supplies `support` for a statement.

- **`Job`:** A `request` to examine specified `evidence` or apply a checked change in fixed limits.

- **Unresolved question:** A recorded question that Muninn cannot safely settle from its current `evidence`.

- **Revision:** A numbered version of a record.

- **Target:** The recorded record and revision that a proposal would replace or affect.

- **Scope:** The global, project, and optional task limits that control where knowledge can apply.

- **Receipt:** The durable database record that confirms which checked changes Muninn recorded.

- **Digest:** A hash that connects an identity to the same stored data.

- **Transaction:** A database operation that records all its changes together or records none.

- **Generation check:** Muninn records a current work version number, called a generation.
  Each `review` `attempt` keeps the generation number it started with.
  When the current number changes, older `attempts` cannot record.
  The generation check compares the attempt's number with the current number.

  The software also names this a generation fence.

For example, one project record says Python 3.12.
A subsequent record says Python 3.13.
If the `evidence` cannot settle the difference, Muninn keeps a question.
The agent can prepare a complete proposal to use Python 3.13 for that project.

Apply the usual `evidence` and owner checks to that proposal.

Global scope is selected explicitly.
Ordinary project reads do not inherit global guidance.
A project scope applies to the specified project.
An optional task limit can limit it more.

These identifiers name records:

| Identifier | Record |
| --- | --- |
| `D_ID` | One complete proposal for an owner decision. |
| `E_ID` | One `evidence` record. |
| `R_ID` | One learning `request` or application `job`. |
| `U_ID` | One unresolved question. |

These are placeholders for actual IDs.
For example, the agent replaces `D_ID` with the displayed proposal's actual ID.

## Context

Muninn records unresolved questions.
A question can have no complete proposed change.
An earlier rejected proposal can also be unavailable.
No such record proves which unchanged change received owner approval.

An agent command cannot prove an actual owner decision.

The owner must be able to give approval, `request` an edit, or reject related memory in conversation.
The agent runs commands and checks the result.
A view alone cannot complete that decision.

## Proposed decision

### Selection and scheduling

Keep ordinary automatic learning free of a new approval requirement.
The `SessionEnd` hook can make a `job` for subsequent `review` without an active conversation.
Do not make an owner decision necessary for each learned item or safely contained proposal error.

Show a decision for an actual unresolved question related to the current task.
Also show a decision when the owner explicitly `requests` `review`.
A subsequent `review` cannot promise `delivery` in the conversation that ended.

The implementer is the person or agent that builds this feature.
The implementer chooses if the work runs during the live conversation or subsequently.
Keep current rules for permitted work and worker ownership.
Limit the wait during the live conversation.

If verification cannot finish in that interval, record pending work for checked continuation.
Do not increase `rounds`, `attempts`, workers, deadlines, or other budgets.

### Save a fixed proposal before display

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

### Put reply instructions in the complete display

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

### Verify the original owner reply

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

### Add only the disclosed owner evidence

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

### Report the actual state

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

### Recheck and save the approved change

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

### Handle later decisions and concurrent application

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

### Settle questions only after a checked save

Checks alone give no approval, permitted `evidence`, knowledge change, or question resolution.
Resolve a question only through a checked application.
The displayed proposed answer must identify and answer that question.
It must account for each affected revision that stays current.

If the change answers only part, keep the unresolved `state` and tell the remainder.
Refusal of a draft does not put back withheld knowledge or necessarily answer its question.
A replacement can make a previous affected revision stale while its historical question stays open.
Keep historical `state` different from current relevance.

### Retain and erase all related records

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

### Upgrade storage without loss of safeguards

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

Keep [ADR 0011](0011-one-way-schema-migration-with-snapshot.md) snapshot protection.
Ship the schema, copier, fingerprints, erasure links, and rollback tests in the same unit.

Keep automatic diagnostics to constant codes, counts, and IDs.
Show proposal and question content only during explicit related `review`.
Keep that content out of logs, doctor output, and automatic startup notices.

## Consequences

Related owner decisions can complete without owner command work.
Ordinary learning stays automatic.
Source waits can keep a decision pending.
A changed target blocks application.

The agent must report that `state` accurately.

Keeping unchanged proposals adds sensitive stored data and maintenance duties.
Proof, erasure, transaction, and schema checks must ship together.
Tests must include the two sequences of concurrent decision and application.
They must also include duplicate `delivery`, crash rollback, source erasure, owner-proof erasure, and maintenance preservation or refusal.

Live host and cross-machine verification stay different from local test results.
