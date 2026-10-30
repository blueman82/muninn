# 0018: Connect owner decisions to exact memory proposals

## Status

ACCEPTED by the owner on 2026-10-29.
Implemented in the private source tree, including commands, checked application, maintenance, and erasure.
Focused checks are recorded in the Task 6 report.
Fresh review and the final full gate remain pending.
Publication, installation, and live verification are separate delivery checks.
Owner authorization: “all above is approved what questions would you ask”.
This approval authorizes implementation.
It does not prove an owner's decision about any runtime memory proposal.

Related proposal: [memory lifecycle design](../memory-lifecycle-design.md).
The separate [ADR 0017 amendment](0017-learning-scope-and-recovery.md#proposed-amendment-bounded-correction-and-action-routing)
covers routine correction and failure notices.

## What the owner will see

After a session ends, Muninn can review its records without an active conversation.
It checks proposed facts or instructions before it saves knowledge.
Ordinary safe learning remains automatic.
The owner does not need to give approval for each memory.

Sometimes, the records leave a real question that Muninn cannot safely answer.
If that question matters to the current task, the agent can ask the owner.
The owner can also request a memory review.
A routine failed proposal does not, by itself, require an owner decision.

The agent first prepares a complete proposed memory change.
It shows what Muninn would save, what it would replace, and which question it would answer.
The same display gives the exact reply sentences with that proposal's ID.
The owner replies in conversation.
The agent checks the original reply and runs the necessary commands.

Muninn then saves the checked change or reports why it remains pending or blocked.
The agent reports “saved” only after Muninn confirms the database change and its receipt.
The owner need not run commands, inspect the database, type JSON, or supply a hash.

## Terms

- **Proposal:** A complete memory change that the owner can review before Muninn saves it.
  The software also calls it a candidate.
- **Evidence:** An original conversation record that supports a statement.
- **Job:** A request to review specified evidence or apply a checked change within fixed limits.
- **Unresolved question:** A saved question that Muninn cannot safely settle from its current evidence.
- **Revision:** A numbered version of a record.
- **Target:** The saved record and revision that a proposal would replace or affect.
- **Scope:** The global, project, and optional task limits that control where knowledge can apply.
- **Receipt:** The durable database record that confirms which checked changes Muninn saved.
- **Digest:** A hash that connects an identity to exact stored data.
- **Transaction:** A database operation that saves all its changes together or saves none.
- **Generation check:** Muninn saves a current work version number, called a generation.
  Each review attempt keeps the generation number it started with.
  When the current number changes, older attempts cannot save.
  The generation check compares the attempt's number with the current number.
  The software also calls this a generation fence.

For example, one project record says Python 3.12.
A later record says Python 3.13.
If the evidence cannot settle the difference, Muninn keeps a question.
The agent can prepare a complete proposal to use Python 3.13 for that project.
That proposal still needs the normal evidence and owner checks.

Global scope is selected explicitly.
Ordinary project reads do not inherit global guidance.
A project scope applies to the specified project.
An optional task limit can restrict it further.

The following identifiers name records:

| Identifier | Record |
| --- | --- |
| `D_ID` | One complete proposal for an owner decision. |
| `E_ID` | One evidence record. |
| `R_ID` | One learning request or application job. |
| `U_ID` | One unresolved question. |

These are placeholders for actual IDs.
For example, the agent replaces `D_ID` with the displayed proposal's actual ID.

## Context

Muninn already records unresolved questions.
A question can lack a complete proposed change.
An earlier rejected proposal can also be unavailable.
Neither record proves which exact change received owner approval.
An agent command cannot prove a genuine owner decision.

The owner must be able to give approval, request an edit, or reject relevant memory in conversation.
The agent runs commands and verifies the result.
A view alone cannot complete that decision.

## Proposed decision

### Selection and scheduling

Keep ordinary automatic learning free of a new approval requirement.
The `SessionEnd` hook can create a job for later review without an active conversation.
Do not require an owner decision for every learned item or safely contained proposal failure.

Present a decision for a genuine unresolved question relevant to the current task.
Also present a decision when the owner explicitly requests review.
A later review cannot promise delivery in the conversation that already ended.

The implementer is the person or agent that builds this feature.
The implementer chooses whether the work runs during the live conversation or later.
Keep existing rules for permitted work and worker ownership.
Limit the wait during the live conversation.
If verification cannot finish within that interval, save pending work for checked continuation.
Do not increase rounds, attempts, workers, deadlines, or other budgets.

### Save a fixed proposal before display

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

### Put reply instructions in the complete display

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

### Verify the original owner reply

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

### Add only the disclosed owner evidence

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

### Report the actual state

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

### Recheck and save the approved change

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

### Handle later decisions and concurrent application

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

### Settle questions only after a checked save

Inspection alone creates no approval, permitted evidence, knowledge change, or question resolution.
Resolve a question only through a checked application.
The displayed proposed answer must identify and answer that question.
It must account for every affected revision that remains current.

If the change answers only part, keep the unresolved state and explain the remainder.
Rejection of a draft does not restore withheld knowledge or necessarily answer its question.
A replacement can make an old affected revision stale while its historical question stays open.
Keep historical state distinct from current relevance.

### Retain and erase all related records

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

### Upgrade storage without loss of safeguards

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

Preserve [ADR 0011](0011-one-way-schema-migration-with-snapshot.md) snapshot protection.
Ship the schema, copier, fingerprints, erasure links, and rollback tests in the same unit.

Keep automatic diagnostics to fixed codes, counts, and IDs.
Show proposal and question content only during explicit relevant review.
Keep that content out of logs, doctor output, and automatic startup notices.

## Consequences

Relevant owner decisions can complete without owner command work.
Ordinary learning remains automatic.
Source delays can leave a decision pending.
A changed target blocks application.
The agent must report that state accurately.

Exact proposal retention adds sensitive stored data and maintenance duties.
Proof, erasure, transaction, and schema checks must ship together.
Tests must cover both orders of concurrent decision and application.
They must also cover duplicate delivery, crash rollback, source erasure, owner-proof erasure, and maintenance preservation or refusal.
Live host and cross-machine verification remain separate from local test results.
