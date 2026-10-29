# 0018: Bind conversational memory decisions to exact candidates

## Status

PROPOSED. Owner approval pending.
This document does not authorize implementation or change an accepted decision.

Related proposal: [memory lifecycle design](../memory-lifecycle-design.md).
The separate [ADR 0017 amendment](0017-learning-scope-and-recovery.md#proposed-amendment-bounded-correction-and-action-routing)
covers routine correction and failure presentation.

## Context

Muninn records unresolved questions but lacks a complete conversational decision path.
A question can lack a complete change. An earlier rejected payload can be unavailable.
Neither record establishes exactly what the owner approved.
An agent command also cannot establish a genuine owner decision.

The owner must be able to approve, edit, or reject relevant memory in conversation.
The agent performs commands and verification. The owner need not run commands, inspect
the database, type JSON, or supply a digest.

## Proposed decision

### Selection and scheduling

Keep ordinary automatic learning ungated. SessionEnd can admit later headless work.
Do not require an owner decision for every learned item or contained proposal failure.
Present a decision for a genuine unresolved question relevant to the current task,
or when the owner explicitly requests review.
A later headless review cannot promise delivery in the conversation that has ended.

The builder chooses foreground or background scheduling within existing admission
and worker ownership. Foreground waiting must be bounded.
Persist pending work for checked continuation when verification cannot finish then.
Do not increase rounds, attempts, workers, deadlines, or other budgets for this path.

### Immutable candidates and complete display

Create a narrow governed candidate and decision relation, with an opaque `D_ID`.
Freeze these candidate fields before display:

- The canonical change payload, scope, original support references, and target revisions.
- The owner-support enrichment version defined below.
- Any originating unresolved `U_ID` and proposed question disposition.

Retain a domain-separated digest over the candidate identity and these frozen fields.

Use `knowledge_write._redacted_body` and `redact_parts` consistently for staging,
display, and application. Normalize before freezing the candidate.
Display content from the durable canonical payload and verify its digest.
Show the complete decision-relevant content, targets, disposition, and support rule.
A truncated or materially incomplete display cannot receive valid approval.

If only a question or claim exists, prepare and display a complete change first.
If an earlier rejected payload is unavailable, disclose that fact and draft anew
from checked evidence. Do not reconstruct an exact proposal from a failure code.

An edit creates a new candidate identity and requires a fresh complete display.
A verified owner edit supersedes the earlier draft.
Never transfer approval to edited content, changed targets, or changed normalization.

### Genuine owner proof

Bind each accepted decision to verified original primary owner evidence and the exact
candidate digest. The owner-facing `D_ID` is a handle, not a substitute for that proof.
Verify the original whole owner field, attribution, and exact candidate reference
through current source checks.

Support a whole affirmative instruction such as “Please approve memory D_ID.”
Use a supported whole-field grammar for affirmative owner intent.
Validate affirmative intent separately from owner authority classification.
Reject negated, conditional, quoted, ambiguous, or conflicting decisions.
A bare “approved” qualifies only when the original record proves its reply binding
to the exact displayed candidate. Otherwise obtain an explicit candidate-bound reply.
Agent statements, CLI calls, recalled text, and unverified wrappers cannot prove approval.
Apply the same source and identity checks before accepting a rejection or edit.

Retain original factual support and later owner-decision evidence as distinct
associations. Approval authorizes the displayed change; it does not prove every claim true.
Reverify both forms of evidence before application.

### Disclosed support enrichment

Freeze the candidate's original supports and enrichment version together.
The display must explain that application adds the real verified owner-decision
`E_ID` to each applicable claim's ordinary support references.
The owner need not know that future evidence ID before deciding.

The versioned rule identifies the applicable support fields, preserves reference
order, and appends the real ID only when absent.
It changes no text, kind, action, scope, target revision, dependency, or expiry.
Keep every original support and validate it against its original source.
The normal authority validator must see actual `direct_owner` evidence in those
ordinary references. A sidecar approval flag cannot bypass authority checks.
Retain the derived payload digest with the decision outcome and apply receipt.
If a changed transform would alter the approved contract, require a fresh candidate.

### Truthful state and checked application

Expose distinct states through agent-facing commands and explanations:

| State | Meaning |
| --- | --- |
| `waiting_verification` | The candidate exists; the claimed owner decision is not yet verifiable. |
| `approved_pending` | Source-bound approval is verified; application has not committed. |
| `saved` | Knowledge and its durable apply receipt committed together. |
| `rejected` | A verified rejection closed this draft without a knowledge write. |
| `blocked` | A specific safety, source, scope, target, setup, or policy refusal prevents progress. |

Keep superseded and erased metadata distinguishable from these active outcomes.
An owner reply, queued worker, or successful command launch cannot establish `saved`.
Do not fabricate evidence while indexing is pending or ask the owner to index it.
No deferred worker may apply an unverified approval.

Create a new foreground-origin checked application request.
The originating unresolved request keeps its immutable receipt.
Background execution retains the verified owner-decision origin.
Recheck original supports, owner proof, current targets, scope, tombstones, policy,
admission, pauses, security bindings, and generation fences at application.
Approval cannot restart paused learning or widen authority or scope.
A changed target blocks application and requires a fresh displayed candidate when appropriate.

Hold at most one writer root and make no model call while holding it.
Commit knowledge changes, receipt, applied decision outcome, and any valid unresolved
resolution in one transaction. Duplicate application returns the existing receipt.
A failure or crash before commit leaves no partial knowledge or false saved state.

### Rejection, supersession, and races

Give decision state a monotonic revision. Each accepted approval, rejection, edit,
or supersession advances it. Preserve earlier accepted decision evidence.
Do not overwrite approval proof when recording a later negative decision.

Inside one `BEGIN IMMEDIATE` application transaction, recheck the candidate digest,
active approval proof, decision revision, and `approved_pending` state.
Use a conditional state update in that transaction with the knowledge and receipt writes.
A verified rejection or supersession committed first fences out old application.
If any check or conditional update fails, roll back knowledge, receipt, applied outcome,
and resolution together. Preserve previously committed decision evidence.

If application commits first, return its receipt.
A later owner change requires a separate checked retraction or replacement.
It cannot make the earlier saved result unsaved.
Late proof cannot reopen rejected, superseded, or erased candidates.
A later approval requires a fresh candidate and display.
Duplicate approve or reject delivery returns the current actual state or receipt.
An unverified negative intent remains `waiting_verification`; it does not establish cancellation.

### Question disposition

Inspection alone creates no approval, evidence admission, knowledge change, or resolution.
Resolve a question only through a checked application whose displayed disposition
identifies that question, answers it, and accounts for every still-current affected revision.
If the change disposes of only part, retain the unresolved state and explain the remainder.
Rejecting a draft does not restore withheld knowledge or necessarily answer its question.
A replacement can make an old affected revision stale while its historical question stays open.
Keep historical state distinct from current relevance.

### Retention, erasure, and maintenance

Retain canonical candidates and accepted decision associations as governed data until
the applicable erasure rules remove them. Link each decision to its candidate digest,
original owner evidence, checked application request, and final receipt when present.
Do not rewrite decisions or receipts to claim approval of a different payload.
Keep original-support and owner-proof associations separately available for validation
and erasure. Avoid duplicate proof text in diagnostics or pending-work records.

Session, event, text-match, and transitive erasure must traverse every new association,
including candidates, decisions, pending work, and dependent knowledge.
Erasing source or owner-proof evidence invalidates dependent content through the governed closure.
Remove erased payload and proof bodies from display records, pending work, and any copies.
Do not reconstruct erased bodies from receipts, evidence, or diagnostic records.
Retained safe metadata must not permit erased content to be displayed or applied.
Keep provider transcripts unchanged.

Register every new schema shape in both applicable fingerprint paths.
Copy and rebuild must preserve its data and erasure associations or safely refuse it.
Include the relation in installer maintenance pauses, snapshots, and rollback validation.
Older tools may refuse the extended schema; they must not silently omit decision data.
Preserve [ADR 0011](0011-one-way-schema-migration-with-snapshot.md) snapshot protection.
Ship schema, copier, fingerprints, erasure closure, and rollback coverage in the same unit.

Keep automatic diagnostics to finite codes, counts, and IDs.
Candidate and question content belongs in explicit relevant review, never logs,
doctor output, or automatic startup notices.

## Consequences

Relevant conversational decisions can complete without owner command work.
Ordinary learning remains automatic. Source delays and changed targets can leave a
decision pending or blocked, and the agent must report that state accurately.

Exact payload retention introduces sensitive governed data and new maintenance obligations.
Approval is safe only when proof, erasure, transaction, and schema checks ship together.
Tests must exercise both race orders, duplicate delivery, crash rollback, source and
owner-proof erasure, and maintenance preservation or refusal.
Live host and cross-machine qualification remain separate from local test results.
