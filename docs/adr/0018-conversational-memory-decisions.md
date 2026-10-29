# 0018: Bind owner decisions to exact memory candidates

## Status

ACCEPTED by the owner on 2026-10-29. Implementation is not yet complete.
Owner authorization: “all above is approved what questions would you ask”.
This approval authorizes implementation. It is not runtime proof of any memory decision.

Related proposal: [memory lifecycle design](../memory-lifecycle-design.md).
The separate [ADR 0017 amendment](0017-learning-scope-and-recovery.md#proposed-amendment-bounded-correction-and-action-routing)
covers routine correction and failure presentation.

## Context

Muninn records unresolved questions but lacks a complete conversational decision path.
A question can lack a complete change. An earlier rejected payload can be unavailable.
Neither record identifies the exact change that received owner approval.
An agent command also cannot establish a genuine owner decision.

The owner must be able to give approval, edit, or reject relevant memory in conversation.
The agent runs commands and verifies the result.
The owner need not run commands, inspect the database, type JSON, or supply a digest.

## Proposed decision

### Selection and scheduling

Keep ordinary automatic learning free of a new approval gate.
SessionEnd can admit later headless work.
Do not require an owner decision for every learned item or contained proposal failure.
Present a decision for a genuine unresolved question relevant to the current task,
or when the owner explicitly requests review.
A later headless review cannot promise delivery in the conversation that has ended.

The builder selects foreground or background execution within existing admission and worker ownership.
Limit the foreground wait.
If verification cannot finish within that interval, save pending work for checked continuation.
Do not increase rounds, attempts, workers, deadlines, or other budgets for this path.

### Immutable candidates and complete display

Create a narrow governed relation between candidates and decisions, with an opaque `D_ID`.
A candidate is a complete proposed memory change.
Governed records remain subject to validation, maintenance, and erasure rules.
Freeze these candidate fields before display:

- The canonical change payload, scope, original support references, and target revisions.
  The canonical payload is the change data after required normalization.
- The owner-support enrichment version defined below.
- Any originating unresolved `U_ID` and proposed question disposition.

Retain a domain-separated digest over the candidate identity and these frozen fields.
The digest binds exact stored data to its identity. Domain separation labels the hash for this record type.

Use `knowledge_write._redacted_body` and `redact_parts` consistently for candidate storage, display, and application.
Normalize the candidate before its fields become fixed.
Display content from the durable canonical payload and verify its digest.
Show the complete decision-relevant content, targets, disposition, and support rule.
A truncated or materially incomplete display cannot receive valid approval.

If only a question or claim exists, prepare and display a complete change first.
If an earlier rejected payload is unavailable, disclose that fact.
Prepare a new draft from checked evidence.
Do not reconstruct an exact proposal from a failure code.

An edit creates a new candidate identity and requires a fresh complete display.
A verified owner edit supersedes the earlier draft.
Never transfer approval to edited content, changed targets, or changed normalization.

### Genuine owner proof

Bind each accepted decision to verified original primary owner evidence and the exact candidate digest.
Primary owner evidence is an original owner statement with verified source and attribution.
The owner-facing `D_ID` identifies the candidate. It does not replace that proof.
Verify the original whole owner field, attribution, and exact candidate reference
through current source checks.

Support a whole affirmative instruction such as “Please approve memory D_ID.”
Use a supported whole-field grammar for affirmative owner intent.
Validate affirmative intent separately from owner authority classification.
Reject negated, conditional, quoted, ambiguous, or conflicting decisions.

A bare “approved” qualifies only when the original record proves that the reply refers to the exact displayed candidate.
Otherwise obtain a reply that explicitly identifies the candidate.
Agent statements, CLI calls, recalled text, and unverified wrappers cannot prove approval.
Before acceptance of a rejection or edit, apply the same source and identity checks.

Retain original factual support and later owner-decision evidence as distinct associations.
Approval authorizes the displayed change. It does not prove every claim true.
Reverify both forms of evidence before application.

### Disclosed support enrichment

Freeze the candidate's original supports and enrichment version together.
The display must explain the support enrichment rule.
Application adds the real verified owner-decision `E_ID` to each applicable claim's ordinary support references.
The owner need not know that future evidence ID before the decision.

The versioned rule identifies the applicable support fields and preserves reference order.
It appends the real ID only when absent.
It changes no text, kind, action, scope, target revision, dependency, or expiry.
Keep every original support and validate it against its original source.

The normal authority validator must see actual `direct_owner` evidence in those
ordinary references. A separate approval flag cannot bypass authority checks.
Retain the derived payload digest with the decision outcome and apply receipt.
If a changed transform would alter the approved contract, require a fresh candidate.

### Truthful state and checked application

Expose distinct states through agent-facing commands and explanations:

| State | Meaning |
| --- | --- |
| `waiting_verification` | The candidate exists. Muninn cannot yet verify the claimed owner decision. |
| `approved_pending` | Muninn verified source-bound approval. Application has not committed. |
| `saved` | Knowledge and its durable apply receipt committed together. |
| `rejected` | A verified rejection closed this draft without a knowledge write. |
| `blocked` | A specific safety, source, scope, target, setup, or policy refusal prevents progress. |

Keep superseded and erased metadata distinguishable from these active outcomes.
An owner reply, queued worker, or successful command launch cannot establish `saved`.
While indexing is pending, do not fabricate evidence or ask the owner to index it.
No deferred worker may apply an unverified approval.

Create a new foreground-origin checked application request.
The originating unresolved request keeps its immutable receipt.
Background execution retains the verified owner-decision origin.
Recheck original supports, owner proof, current targets, scope, tombstones, policy,
admission, pauses, security bindings, and generation fences at application.
Approval cannot restart paused learning or widen authority or scope.
A changed target blocks application and requires a fresh displayed candidate when appropriate.

Hold at most one writer root.
The writer root is the outer operation that holds the shared database write lock.
While it holds that lock, make no model call.
Commit knowledge changes, receipt, applied decision outcome, and any valid unresolved
resolution in one transaction. Duplicate application returns the existing receipt.
A failure or crash before commit leaves no partial knowledge or false saved state.

### Rejection, supersession, and races

Give decision state a revision that only increases.
Each accepted approval, rejection, edit, or supersession advances it.
Preserve earlier accepted decision evidence.
When the owner gives a later negative decision, do not overwrite earlier approval proof.

Inside one `BEGIN IMMEDIATE` application transaction, recheck the candidate digest,
active approval proof, decision revision, and `approved_pending` state.
Use a conditional state update in that transaction with the knowledge and receipt writes.
A verified rejection or supersession committed first prevents the old application.
If any check or conditional update fails, roll back knowledge, receipt, applied outcome,
and resolution together. Preserve previously committed decision evidence.

If application commits first, return its receipt.
A later owner change requires a separate checked retraction or replacement.
It cannot make the earlier saved result unsaved.

Late proof cannot reopen rejected, superseded, or erased candidates.
A later approval requires a fresh candidate and display.
Duplicate delivery of approval or rejection returns the current actual state or receipt.
An unverified negative intent remains `waiting_verification`. It does not establish cancellation.

### Question disposition

Inspection alone creates no approval, evidence admission, knowledge change, or resolution.
Resolve a question only through a checked application.
Its displayed disposition must identify and answer that question.
It must account for every affected revision that remains current.

If the change disposes of only part, retain the unresolved state and explain the remainder.
Rejection of a draft does not restore withheld knowledge or necessarily answer its question.
A replacement can make an old affected revision stale while its historical question stays open.
Keep historical state distinct from current relevance.

### Retention, erasure, and maintenance

Retain canonical candidates and accepted decision associations as governed data until applicable erasure rules remove them.
Link each decision to its candidate digest, original owner evidence, checked application request, and final receipt when present.
Do not rewrite decisions or receipts to claim approval of a different payload.
Keep original-support and owner-proof associations separately available for validation
and erasure. Avoid duplicate proof text in diagnostics or pending-work records.

Session, event, text-match, and transitive erasure must traverse every new association,
including candidates, decisions, pending work, and dependent knowledge.
Erasure of source or owner-proof evidence invalidates dependent content through the governed closure.
Remove erased payload and proof bodies from display records, pending work, and any copies.
Do not reconstruct erased bodies from receipts, evidence, or diagnostic records.
Retained safe metadata must not permit erased content to be displayed or applied.
Keep provider transcripts unchanged.

Register every new schema shape in both applicable fingerprint paths.
Copy and rebuild must preserve its data and erasure associations or safely refuse it.
Include the relation in installer maintenance pauses, snapshots, and rollback validation.
Older tools may refuse the extended schema. They must not silently omit decision data.

Preserve [ADR 0011](0011-one-way-schema-migration-with-snapshot.md) snapshot protection.
Ship schema, copier, fingerprints, erasure closure, and rollback coverage in the same unit.

Keep automatic diagnostics to finite codes, counts, and IDs.
Candidate and question content belongs in explicit relevant review, never logs,
doctor output, or automatic startup notices.

## Consequences

Relevant conversational decisions can complete without owner command work.
Ordinary learning remains automatic.
Source delays and changed targets can leave a decision pending or blocked.
The agent must report that state accurately.

Exact payload retention introduces sensitive governed data and new maintenance obligations.
Approval is safe only when proof, erasure, transaction, and schema checks ship together.
Tests must exercise both race orders, duplicate delivery, and crash rollback.
They must also exercise source and owner-proof erasure, and maintenance preservation or refusal.
Live host and cross-machine qualification remain separate from local test results.
