# Improve the existing Muninn memory lifecycle

Status: Revised proposal, ready for approval after independent design review. The earlier approval request is superseded.
The owner requested conversational approve, edit, and reject actions and delegated scheduling choices to the implementation.
Implementation requires approval of this revised design and its ADR changes.

Implementation baseline: Muninn `a5ff0733a5ba5ec37febacd5445e4c10fc59b684`,
including the reviewed PR #76 context fix. Initial research used `56d1977`.
These proposed lifecycle changes are not implemented.
Upstream reference: Hermes Agent
`fcc26fbd9f1641755e49793d7ed608f3b7214c3d`, inspected on 29 October 2026.
Local HyperSkill reference: clean checkout at `a34978aa7439d2eff7ccc3d2ad5372b34d285bd5`.
This design compares source behavior. It does not claim live qualification of either system.

## Current proposal summary

1. Permit one safe correction call within a remaining existing review round.
   Amend ADR 0017 narrowly; retain two rounds, three attempts, four workers, and all evidence checks.
2. Permit only format correction or removal of unchanged canonical candidates after specified proposal failures.
   A rejected preference cannot become an accepted fact through relabeling.
3. Stop repeating contained request failures at SessionStart.
   Keep doctor and historical inspection complete; preserve startup notices for real owner action.
4. Report fixed failure phases and durable correction outcomes using existing tables.
   These correction and diagnostic changes require no schema additions, provider, store, or daemon.
5. Expose unresolved questions and let the owner approve, edit, or reject relevant memory in this conversation.
   The agent handles commands and validation. Approval binds the exact shown candidate; application can complete in the background.

## Purpose

Reduce avoidable learning failures and repeated owner work in the existing Muninn lifecycle.
Keep original-source verification, evidence attribution, scope, authority, and erasure checks in force.
Use Hermes and HyperSkill as reference systems. Add only mechanisms that fill demonstrated Muninn gaps.

The intended outcome is a contained learning service that can correct safe proposal errors within its existing allowance.
When correction fails, the owner can inspect the result without repeatedly dismissing an ordinary failure notice.
Failures that require account access, security action, or approval must remain visible.

## Existing lifecycle and reference patterns

| Stage | Existing Muninn behavior | Hermes reference | Design decision |
| --- | --- | --- | --- |
| Capture and history | Read-only provider ingestion, transcript search, event opening, and session browsing already exist. | Native session search uses FTS and progressive discovery, scrolling, reading, and browsing. | Keep existing search and opening paths. No second archive, vector index, or search daemon. |
| Select durable knowledge | Governed notes carry facts, preferences, decisions, and other supported kinds. Guides hold procedures and lessons. | `USER.md` holds user facts; `MEMORY.md` holds environment facts. Task procedures belong in skills. | Keep Muninn's richer note and guide model. No duplicate user/environment files. |
| Admit learning | SessionEnd, PreCompact, reconciliation, and checked history admission create bounded requests. History confirmation binds original bytes. | Post-response review forks inspect conversation snapshots. Explicit `/refine` differs from automatic review. | Keep existing triggers and immutable request identity. Do not add another review worker. |
| Generate and correct | Controlled reads and final results share a bounded round protocol. Invalid model results currently end the attempt. | Memory tools return useful repair context and stop after consecutive consolidation failures. | Add narrow correction within a remaining existing round, subject to the rules below. |
| Validate and commit | Original bytes, admitted evidence, authority, scope, current revisions, and tombstones are checked before an atomic commit. | Batches validate their final size; approval targets pin the selected entry. | Preserve Muninn's stronger evidence checks. Share candidate checks without bypassing the commit transaction. |
| Supply context | Startup includes complete eligible notes and a compact guide index. Prompt recall selects scoped context within byte limits. | Small curated blocks use frozen prompt snapshots and character caps. Compression refreshes snapshots. | Keep Muninn's bounded, scoped delivery. No global prompt snapshot change without a measured freshness or cache problem. |
| Maintain knowledge | Revisions, conflicts, unresolved items, expiry, dependency checks, tombstones, and erasure already exist. | Curated memory supports add, patch, and remove. External providers have separate lifecycle hooks. | Keep existing maintenance semantics and default automatic learning. Evaluate only the missing conversational decision behavior. |
| Inspect unresolved questions | Questions and claims are stored, but ordinary read commands expose unresolved IDs or counts without their content. | Pending memory proposals have explicit inspection commands. | Add scoped inspection and specify conversational approve/edit/reject without inventing new owner authority. |
| Explain failures | Safe codes are recorded; `learn show`, explanations, and doctor expose diagnostics. Startup selects a saved issue. | Review notifications distinguish selected pending proposals from successful writes. | Separate ordinary contained failures from issues that require owner action. Preserve inspection access. |

Muninn source anchors:
[knowledge models](../muninn/knowledge_models.py),
[bounded startup context](../muninn/knowledge_push.py),
[scoped recall](../muninn/hook_recall.py),
[capture](../muninn/hook_learn.py),
[history admission](../muninn/review_history.py),
[original evidence](../muninn/review_evidence.py),
[atomic commit](../muninn/review_commit.py), and
[erasure](../muninn/knowledge_erase.py).

The table identifies functional equivalents. It does not establish that their policies, coverage, or operational qualification are identical.

## Ranked gaps confirmed in current source

### 1. Safe correction stops too early

`review_supervisor_result.execute` sends model parsing errors directly to the failure callback.
It also maps native output failures to a broad rejection code.
The existing round mechanism continues after a checked read, but cannot return safe validation feedback for correction.

This is a narrow opportunity. It is not permission to let a model change a rejected claim until it passes.
For example, authority checks depend partly on the declared knowledge kind.
An unrestricted correction could relabel a preference as a fact after an authority rejection.
That would evade the intended boundary.

Sources: [result reconciliation](../muninn/review_supervisor_result.py),
[round validation](../muninn/review_rounds.py), and
[candidate authority checks](../muninn/review_commit.py).

### 2. Ordinary contained failures can become repeated owner reminders

`review_recovery.notice` selects the setup issue first, then the latest saved request issue.
It does not distinguish a request that safely ended from a failure that needs owner action.
SessionStart can therefore repeat an ordinary model rejection until someone dismisses its record.
The diagnostic record is useful. Its repeated presentation is the gap.

Sources: [issue selection](../muninn/review_recovery.py) and
[SessionStart use](../muninn/hook.py).

### 3. Failure detail loses the relevant phase

Result reconciliation groups parsing, controlled reads, evidence checks, and commit rejection into broad failure handling.
The supervisor callback defaults the diagnostic stage to `review`.
That makes a malformed model response harder to distinguish from an evidence refusal or a storage problem.
Some older attempts have no detailed stage at all.

Sources: [result reconciliation](../muninn/review_supervisor_result.py),
[supervisor failure recording](../muninn/review_supervisor.py), and
[historical detail fallback](../muninn/review_recovery.py).

### 4. Stored unresolved questions cannot be read through ordinary commands

`save_unresolved` stores the question, disputed claim, affected revisions, and support references.
Startup supplies unresolved IDs. Request views expose receipt IDs or counts.
There is no `U_ID` read path in the current knowledge parser or handler.
The owner must not need direct database access to understand a withheld decision.

The existing `unresolved_ids(scope)` query is deliberately narrower than a general question list.
It finds open questions affecting current applicable revisions and omits allowed questions with no affected revisions.
A new inspection path must use the originating request's scope, not reuse that query as its authorization boundary.
Inspection alone does not meet the owner's clarified request to approve, edit, or reject memory in this conversation.
The complete conversational decision contract below fills that separate gap without gating ordinary automatic learning.

Sources: [question persistence](../muninn/knowledge_write.py),
[current relevance query](../muninn/knowledge_read.py),
[knowledge parser](../muninn/cli_parser.py), and
[knowledge handler](../muninn/cli_know.py).

## Recommended additions

### A. Bounded proposal correction

Use the request's existing `max_rounds` allowance. The default remains two rounds.
Do not create another attempt, extend the deadline, increase worker count, or change request scope.
If no round remains, record a contained failure and continue unrelated requests.

There are two distinct correction cases:

1. **Format correction:** A bounded model operation passes native decoding and strict JSON decoding but fails local union or schema validation.
   Before validation succeeds, do not trust an `op` discriminator or call the payload a final result.
   An unknown operation or invalid schema shape qualifies for fixed, program-owned format feedback.
   The next operation must be a valid final result in the remaining last round.
   Invalid native framing or payload JSON, native tool events, oversized output, and runner failures are not format correction cases.
2. **Proposal reduction:** A fully typed final contains a candidate that fails an eligible candidate check.
   The corrected final may remove candidates or return no change.
   Every surviving candidate must equal its original canonical representation.
   It cannot change text, kind, action, scope, target, revision, evidence references, dependencies, or expiry.
   It cannot introduce a new claim or unresolved item.

The second rule is monotonic: correction can reduce a proposed batch, but cannot expand its meaning or authority.
Require a strict ordered canonical subset, with at least one candidate removed.
Apply the same restriction to both changes and unresolved items, or permit an empty result with a fixed no-change reason.
Return a typed candidate position and a fixed failure code; never return arbitrary exception text.
Do not echo rejected model text into instructions or logs.

The eligible candidate failures are limited to candidate scope mismatch, missing direct-owner support,
a repeated replacement target, or an overlap between replacement targets and affected unresolved targets.
A scope mismatch permits dropping that candidate only. It never grants access to a wider scope.
Authority failures never permit relabeling or new support references.
Changed originals, erased evidence, unavailable evidence, invalid read scope, uncertain process state, and broken security bindings remain terminal for that result.
An evidence-read limit is not a memory-capacity error. Do not retry it unchanged.

Use a typed `ProposalRejectedError` for exactly these candidate failures inside the existing validation path.
After all original-evidence checks pass, the supervisor can request deferral of that typed failure.
Add a default-off `defer_candidate_failure` option to the existing commit path.
Only the supervisor may opt in, and only when one correction round remains.
The rejected transaction rolls back without governed writes or terminal attempt bookkeeping.
All other callers and errors retain their existing behavior.
Unknown evidence references, invalid targets, stale target revisions, policy changes, and generation fences remain terminal.

Keep the original candidate batch in transient `Round` state for the strict subset check.
Do not persist rejected model content as a diagnostic or treat it as evidence.
The corrected commit repeats all current checks against durable state and original bytes.
Use no extra preflight evidence read and no nested writer lock; the shared writer lock is not reentrant.
One correction call is allowed at most. A preceding read round can consume that opportunity.
The corrective stage accepts a final operation only; it cannot request another controlled read.
Identify format correction at the decoding/schema boundary and proposal reduction through its typed candidate error.
Never convert a controlled-read or commit safety refusal into format feedback by matching exception strings.

Before every correction call, refresh the active request and admitted evidence union.
Recheck policy, generation, round allowance, original-read budgets, deadline, and existing knowledge freshness rules.
Retain the same permit and attempt accounting.
If prompt assembly cannot include bounded correction feedback, stop safely rather than silently dropping required context.

### B. Owner-action selection

Classify saved issues with a fixed code policy at read time.
Reuse the existing issue table and inspection commands where possible.
A contained request failure remains available through `learn show` and its explanation.
It does not require a dismissal to keep later SessionStart hooks quiet.

Keep these states visible at startup:

- Sign-in or account access is required.
- An approved capability, host, launch input, or policy binding cannot be verified.
- A review reported a prohibited native tool event.
- A previous process cannot be proved stopped.
- Learning remains paused or requires an existing owner approval action.
- An unknown failure cannot safely be classified as contained.

Ordinary proposal rejection, exhausted format correction, and exhausted safe retries do not require owner action when admission remains safe.
A deterministic source-read limit remains inspectable with its specific cause and maintainer action.
It must not become a repeated request to rerun unchanged work.
Do not hide an active shared setup failure merely because a newer request failure exists.

Add a startup-only action selector. Preserve the existing `notice()` selection and doctor's complete attention semantics.
Routine failures remain visible in doctor, request history, and explanations.
Do not report doctor as healthy merely because startup no longer repeats an ordinary request failure.
Deriving presentation from durable state avoids a new delivery ledger, expiry policy, or dismissal queue.

### C. Safe phase diagnostics

Record a fixed phase for new failures: native output, model format, candidate validation,
controlled read, original evidence, or commit.
Keep the existing finite error code when it carries a more specific cause.
Do not store transcript text, model content, raw validation messages, or provider secrets in diagnostics.

Expose the phase through existing request detail and explanation output.
Use `not recorded` for old attempts without evidence. Do not reconstruct historical phases by inference.
Use finite program-owned `review_call.outcome` values to mark a completed call that requested correction.
Persist the marker atomically after checking the completed call, current attempt, policy, and generation fence.
Keep existing terminal-call state, usage, and charge accounting unchanged.
A later launched correction call establishes that correction was attempted.
A final failure plus the marker and completed correction call can establish exhaustion.
Two calls alone do not prove correction; the first call may have requested a controlled read.
Use `unknown` when historical durable records cannot establish the distinction.
Add no schema field or table. Transient `Round` data controls live subset comparison but cannot prove historical correction.

### D. Unresolved question inspection and conversational decisions

Add `muninn know unresolved [U_ID] [--human]`.
Without an ID, list applicable unresolved records within a bounded page.
With an ID, show the stored question, claim, affected revisions, support references, and originating request.
Default output is JSON. `--human` gives an explicit explanation for a person.
Do not change the content or state of an unresolved record by reading it.

Scope list discovery through the originating request's `scope_json` and existing project/global scope rules.
Include records with an empty affected list. Do not infer their scope through affected knowledge alone.
An explicit `U_ID` opens that exact historical record, matching `know show` identity semantics.
It grants no evidence read admission and makes no claim of current support.
An erased record or null `item_json` returns safe state metadata with no body.
Do not reconstruct erased questions from receipts, evidence, or old model output.
Apply existing redaction and output bounds. Malformed records fail safely without echoing raw stored content.
Keep question or claim content out of logs, doctor, and automatic startup notices.
An unknown ID returns a safe not-found response.

Distinguish current relevance from historical state.
A checked replacement can activate revision N+1 of a withheld target.
An unresolved item affecting only revision N no longer withholds that current replacement.
The original item can remain historically open. Show its affected revision as stale rather than implying automatic resolution.

Opening a question grants no new evidence or authority, and the question itself remains a model proposal.
The owner chooses approve, edit, or reject in the conversation. The agent performs the necessary command operations.
Do not ask the owner to inspect databases or run a command to complete that choice.

#### Timing and selection

Default automatic learning remains ungated. Do not ask the owner to approve every learned item or every failed proposal.
Offer a decision only for a genuine unresolved item relevant to the current task, or when the owner explicitly requests a review.
Contained format, validation, and budget failures remain machine or maintainer work unless they expose a real owner decision.

SessionEnd records a boundary for later headless learning. The resulting review need not finish before that conversation ends.
A headless result cannot assume that it can reopen the original live conversation and obtain an answer there.
A later relevant foreground session can inspect the unresolved item and discuss it with the owner.
An explicit review during a live conversation can also prepare and display a candidate then.
Do not promise same-session decision delivery from a later SessionEnd review.

#### Exact candidates and owner choices

Approval authorizes only the exact displayed immutable candidate. It does not promise immediate scheduling.
Create one narrow governed candidate/decision relation for this purpose; do not create a general inbox or a new memory backend.
Its immutable candidate data includes an opaque `D_ID`, canonical change payload, scope, original support references,
target revisions, an explicit owner-support enrichment version, optional originating unresolved ID and proposed question disposition,
and a domain-separated digest over those fields.
The human-facing ID is a handle. The owner does not type a hash, JSON, or CLI command.
Use the same `knowledge_write._redacted_body` and `redact_parts` normalization for staging, display, and application.
Normalize before freezing the candidate. Persist it before displaying text derived from that stored payload.
Reject a truncated or materially incomplete approval display; the owner must receive the complete decision-relevant content and target information.
The display discloses the one allowed apply-time transformation: the verified owner-decision `E_ID` will join each applicable claim's supports.
The owner need not know that future evidence ID before it exists.

An edit creates a fresh candidate identity. A verified owner edit durably supersedes the earlier draft.
Display the new payload before asking for approval.
Never transfer an earlier approval to edited text or a changed target.
If an unresolved record contains only a question or claim, first draft and display a complete change.
If the rejected proposal was not retained, say that it is unavailable and prepare a fresh draft from checked evidence.
Do not reconstruct an exact historical proposal from a broad failure code or treat a question as an approved change.

The owner chooses approve, edit, or reject in the conversation. The agent performs the command mechanics.
An approval must bind a genuine primary owner statement to the candidate.
Use an existing supported direct-instruction form, such as “Please approve memory D_ID.”
Verify the original whole owner field, its attribution, and its exact candidate reference through current source checks.
Validate affirmative decision intent with the supported whole-field grammar, separately from authority classification.
Reject negated, conditional, quoted, or conflicting decisions even if the source classifier reports owner authority.
A bare “approved” requires proven reply binding; without it, the agent obtains an explicit candidate-bound instruction.
An agent-generated statement, CLI invocation, or unverified wrapped reply cannot prove a human decision.

Keep the candidate's original factual support separate from later owner-decision evidence.
Freeze the original payload and transform version together; do not mutate that candidate after approval.
After verifying the real owner record, derive the application payload by adding its `E_ID` to every applicable claim support list.
The versioned rule defines those fields, preserves existing reference order, and appends the real ID only when absent.
It changes no text, kind, scope, action, target revision, dependency, or expiry.
Original supports remain present and must still pass their original-source checks.
The normal commit validator sees the actual `direct_owner` owner-statement evidence in ordinary support references.
Use no separate authority bypass and no inference that a CLI invocation proves approval.
Approval authorizes the candidate and disclosed support transformation; it does not prove every factual claim true.
Record the derived payload digest with its decision outcome and receipt for exact replay verification.
If a changed redaction or transformation contract would alter approved content, require a fresh candidate.
Record reject against the exact candidate only after verifying the genuine owner decision; it authorizes no knowledge write.

#### Verification, application, and honest status

Use existing background execution where practical. The builder chooses exact scheduling after inspecting current worker ownership and admission.
The foreground agent can wait a bounded period for the owner record to become indexed and verifiable.
It must not loop indefinitely, invent evidence, or ask the owner to perform indexing or database commands.
If verification or application cannot finish in that interval, persist the checked pending state for normal background continuation.
No deferred worker may apply a candidate before genuine decision proof is verified.

Expose these distinct states through the existing agent-facing command and explanation surfaces:

- `waiting_verification`: the displayed candidate exists, but its owner decision is not yet verifiable.
- `approved_pending`: source-bound approval is verified; application has not committed.
- `saved`: the exact candidate has a durable apply receipt and its knowledge transaction committed.
- `rejected`: a verified rejection closed that draft without a knowledge write.
- `blocked`: verification or application encountered a specific safety, scope, target, setup, or source refusal.

Keep superseded and erased candidate metadata distinguishable from these active outcomes.
Report pending or blocked honestly. Do not say “saved” because the owner answered or a worker was queued.
Retain finite codes and IDs in automatic diagnostics; show candidate content only through an explicit relevant review.

Each candidate's decision state has a monotonic revision. Every accepted approval, rejection, edit, or supersession advances it.
Preserve earlier approval proof when recording a later negative decision; do not overwrite the history of accepted evidence.
At application transaction start, recheck the exact candidate digest, active approval evidence, decision revision, and `approved_pending` state.
Use a conditional state update inside the same `BEGIN IMMEDIATE` transaction as knowledge and receipt writes.
A verified rejection or supersession durably recorded before application commits cancels the old application.
If any fence fails, roll back the entire transaction without a partial write or receipt.

Delayed approval verification cannot reopen a rejected, superseded, or erased candidate.
A later approval needs a fresh draft and display. Repeated delivery of the same decision returns the current actual state or receipt.
An unverified reject or edit is pending intent, not an accepted revocation; report `waiting_verification`, not canceled.
If application committed first, return its existing receipt. A later owner change requires a separate checked retraction or replacement.
Do not imply that already committed memory became unsaved because a later rejection arrived.

Use a new foreground-origin checked application request because the originating unresolved request already has an immutable receipt.
The request can execute later in the background, but retains the verified owner-decision origin.
Recheck original factual supports, owner proof, current target revisions, scope, tombstones, policy, admission, and generation fences.
Approval cannot enable paused learning or bypass a security binding.
If current targets change, block application and prepare a fresh candidate for display and approval when appropriate.

Hold at most one writer root. Make no model call while holding it.
Commit knowledge changes, the apply receipt, the candidate's applied outcome, and valid unresolved resolution atomically.
Repeated application returns the existing receipt rather than duplicating a change.
A crash before commit leaves no partial saved knowledge or false resolved state.

Resolve an unresolved item only when the checked application actually disposes of its question under the new ADR's explicit rule.
An explicit decision must identify the originating question and the approved disposition; a changed target alone is insufficient.
The displayed disposition must answer that question and account for every still-current affected revision.
If only part is disposed of, retain the original unresolved state and show the remaining issue.
Rejecting a draft does not restore an old withheld revision and does not necessarily answer the underlying question.
A replacement can make an old affected revision stale while its historical question remains open.
Preserve that distinction in the result and display.

#### Retention, provenance, and maintenance

The new ADR defines the narrow candidate/decision relation, retention, decision provenance, disclosed support enrichment, resolution rules, and schema compatibility.
Bind each decision to its candidate digest, original owner evidence, checked application request, and final receipt when present.
Payload edits require a new candidate; decisions and receipts cannot be rewritten to claim a different payload was approved.
Keep original support associations and owner-decision associations available separately for validation and erasure.

The relation participates in session, event, text-match, and transitive erasure.
Erasing source or owner-decision evidence invalidates dependent content through the governed erasure closure.
Erased payloads and proof text must not survive in candidate display records, pending work, or diagnostic copies.
Include every new shape in schema fingerprints, copy, rebuild, installer snapshots, and rollback qualification.
No approval-specific table may escape these existing maintenance contracts.

## Hermes behaviors to use carefully

Hermes returns current entries for an initial capacity failure and gives exact correction instructions.
Its fourth consecutive consolidation failure ends repair for that turn.
Success resets the failure counter and says the write is complete.
This supports useful feedback and finite work; it does not prove all memory failures resolve autonomously.

Hermes approval replay pins an exact entry and refuses stale targets.
Its unattended review stages patch/remove proposals independently of the optional write-approval setting.
Explicit `/refine` marks a review attended, while preserving its background-review origin.
Muninn already has policy, authority, and immutable revision checks.
A conversational decision feature reuses these checks and adds the missing exact-decision binding independently of application timing.

Hermes `stage_write` catches a disk-write failure and still returns a record.
Callers can consequently report a staged proposal that was not persisted.
Do not copy this behavior. A saved or completed status must follow durable confirmation.

Hermes summary extraction skips inherited tool results and does not present selected staged proposals as applied changes.
Printing or invoking a callback does not establish durable notification delivery.
The inspected auxiliary-failure warning path does not itself suppress identical failures.
Muninn should reduce repeated owner reminders through explicit state classification, not a claim that Hermes guarantees deduplication.

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

**Copy curated files and hard character limits:** Declined. Muninn already has governed notes, guides, scope, and bounded delivery.
A small prompt block is not a substitute for evidence or a reason to delete supported knowledge.

**Add semantic retrieval or an external provider:** Deferred until measured retrieval failures justify it.
Hermes external providers can receive queries and transcript messages; provider boundaries and deletion behavior differ.
Its provider manager can wait eight seconds for prefetch, and built-in egress scrubbing has limited coverage.
The bundled RetainDB remote mirror does not propagate local deletes. Do not generalize this to every provider.
Adding that architecture would require a separate privacy, erasure, and operational design.

**Adopt progressive history retrieval:** Already present through search, open, and session commands.
Hermes also has content and result-count limits, but a scroll result does not establish a universal payload cap.
No replacement retrieval system is justified by this comparison.

**Trust all recalled text as authoritative:** Declined. Recall is context, not new owner authority.
Keep Muninn's evidence types, supported claims, and current validation boundaries.

**Retry every failed proposal with new wording:** Declined. It can consume allowance and evade a semantic guard.
Use the narrow format and reduction cases above.

**Use format correction only:** Smaller, but leaves valid batches stranded behind one unsupported candidate.
The selected reduction mechanism can retain useful unchanged candidates without accepting revised claims.

**Automatically commit the valid subset:** Not selected. It changes the meaning of an all-or-nothing proposal without model acknowledgment.
A bounded reduction round preserves an explicit final result and supports a deliberate no-change outcome.

**Create new request retries or larger limits:** Declined. Existing limits already bound calls and retries.
Use a remaining round; preserve the three-attempt and four-worker maxima.

**Add a new notification queue:** Not justified by current evidence. Existing durable issues and inspection paths can support action selection.
The selected exact-candidate decision relation is distinct from notification delivery and has the narrow contract above.

## HyperSkill reference and limits

HyperSkill extracts procedural skills after successful tasks and mistakes after failed tasks.
Its prompts ask for WHAT, WHEN, and WHY. This is a useful rubric for checking guide quality.
It does not itself establish that a generated procedure is supported by independent evidence.
The parser checks a skill name but can accept empty content.
Muninn's `GuideBody` and `NoteBody` already express these dimensions through supported content, conditions, reasons, steps, and pitfalls.
Supported pitfalls and decisions already cover lessons from mistakes.
Its original-source associations are stronger than retrieved-memory feedback alone.
No extra procedural runtime feature follows from this comparison.

HyperSkill's graph records undirected episode membership.
It is not a prerequisite graph, a dependency graph, or a skill hierarchy.
Retrieval combines task and plan embeddings with co-occurrence ranking.
Whole-task success credits all retrieved nodes and incident episodes, without proving which procedure was used.
Do not copy this credit as verified utility or evidence promotion.

Its merge and pruning paths overwrite or delete memory without Muninn's revision history.
Recalled guidance can enter later extraction again without an independent contamination check.
A missing judge result becomes `is_correct=False`, which can trigger failure-conditioned extraction.
These behaviors argue for preserving Muninn's evidence origin and checked learning boundaries.

The checkout has no tracked test suite. Its online evaluation changes memory by default.
It supports `disable_memory_evolution` and isolated `storage_prefix` values.
For any future procedural-memory experiment, separate fixed-store retrieval evaluation from store-evolution evaluation.
Do not claim a retrieval gain from an experiment that silently changes the store between comparisons.

No graph, embedding provider, merge policy, or automatic trust promotion is proposed here.
Muninn already distinguishes recall exposure and self-report from comparator evidence.
Its offline replay and learning evaluation support independent labels, family partitions, and frozen held-out cases.
The existing procedural-paraphrase fixture covers literal, paraphrased, and omitted exposure.
Do not describe these controls as missing.

Add evaluation coverage for condition-preserving generalization within the existing fixture framework.
Use a family with observed success under condition A, a paraphrase that retains A, and a related task without A.
Include a failed run with unknown cause and repeated recalled advice.
Check that recalled advice alone does not become independent support, and that unknown failure cause does not become a proved pitfall.
This is additional evaluation coverage, not a demonstrated runtime defect or measured benefit.
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

[ADR 0017](adr/0017-learning-scope-and-recovery.md) currently says:
“Reject deterministic invalid results once.”
Owner approval is required for the [proposed narrow amendment](adr/0017-learning-scope-and-recovery.md#proposed-amendment-bounded-correction-and-action-routing)
before correction is implemented.
The amendment must identify eligible format failures and monotonic candidate reduction explicitly.
All other deterministic refusals retain their existing terminal behavior.
The approved maximum remains two rounds, three total attempts, and four workers.
Existing transient backoffs remain unchanged.

The [proposed ADR 0018](adr/0018-conversational-memory-decisions.md) covers exact candidate retention,
owner-decision provenance, authorization, real question resolution, and schema maintenance.
This ADR authorizes exact source-bound decisions and the disclosed support transformation.
It does not create a separate authority override or make recalled text or CLI execution into owner evidence.

Preserve policy binding, runner proof, child containment, owner pauses, generation fencing,
immutable request admission, original verification, scope, authority, tombstones, and atomic commit.
Do not alter provider transcripts or inspect private archives for this work.
Do not add provider tools, arbitrary filesystem access, shell access, or model-controlled executable feedback.
Do not weaken the native event contract to make malformed runner output repairable.

When command answer fields change, update `docs/REFERENCE.md` and both command-skill mirrors together.
New diagnostics remain JSON on stdout. Provider hooks retain compact JSON and exit zero.
No claim of universal authorship attribution, zero failures, or live cross-platform qualification is part of this design.

## Delivery units

1. **Routine failure handling:** implement bounded correction, startup-only action routing, and safe phase diagnostics with the narrow ADR 0017 amendment.
   Include the additional HyperSkill-inspired evaluation family as test coverage, without claiming a runtime improvement.
   This unit uses existing tables and does not depend on the new conversational decision relation.
2. **Inspection and decision completion:** ship unresolved inspection together with exact candidate creation, owner-proof verification,
   background-safe checked application, receipts, valid resolution, and the new ADR.
   Include schema fingerprints, copy/rebuild, installation rollback, and full erasure closure in the same reviewed unit.
   Do not ship a view-only feature as completion of the owner's approve/edit/reject request.

Each unit has its own focused checks and final full gate. Do not split the decision schema from its maintenance or erasure obligations.

## Acceptance tests

1. A supported format error in the first round receives fixed feedback and can complete in the second round.
   The same failure in the final round ends the attempt without another call.
   Unknown operations and schema-invalid shapes qualify only after native and strict JSON decoding pass.
   A corrective controlled-read operation is rejected. Read and commit safety refusals never become format retries.
2. Correction preserves request identity, attempt identity, deadline, scope, permit accounting, and original-read limits.
   Two rounds, three attempts, and four workers remain enforced under correction and concurrency.
3. A reduction may drop an invalid candidate while preserving valid canonical candidates exactly.
   Rewriting content, kind, target, revision, scope, support, expiry, or dependencies is rejected.
   Relabeling an unsupported preference as a fact cannot pass the reduction boundary.
4. An empty reduction produces the existing no-change outcome.
   No partial knowledge changes occur before a valid final commit.
5. Changed originals, erased evidence, invalid authority, widened scope, and stale revisions retain current enforcement.
   A deferred rejection cannot authorize a later commit after state changes.
   Unknown evidence and invalid target state remain terminal, rather than becoming candidate-pruning opportunities.
6. Native tool events, uncertain processes, invalid security bindings, input/output limits, and identical deterministic read limits do not trigger correction calls.
7. Exhausted ordinary failures remain available in doctor, request inspection, and explanations.
   Repeated SessionStart hooks do not demand dismissal of these contained failures.
8. Sign-in, security, approval, owner-pause, uncertain-process, and unknown action-required failures remain visible.
   Shared setup failures are not obscured by later ordinary request failures.
9. New diagnostics report the observed phase and finite code without transcript/model text or raw exceptions.
   Old records retain `not recorded` where detail is absent.
   Correction markers survive normal completion, while absent markers and two-call read sequences do not produce invented correction history.
10. The unresolved list exposes applicable questions, including empty affected lists, through originating-request scope.
    Explicit `U_ID` shows the selected historical record without granting evidence admission or current support.
    Erased/null bodies are absent and never reconstructed. Malformed records do not leak raw content.
    An old affected revision is marked stale after a checked replacement without changing historical question state.
    Reading a question creates no owner approval, evidence, or knowledge mutation.
11. Extend the existing procedural evaluation fixtures with the condition-preserving generalization family described above.
    Keep independent labels, family separation, and frozen held-out comparisons.
    Report results as added coverage until measurements establish a runtime quality problem or benefit.
12. Ordinary automatic learning commits without a new approval gate. Routine rejected proposals do not automatically become owner decisions.
    Relevant genuine unresolved items and explicit owner review can create and display checked immutable candidates.
    A question without a complete change and an unavailable historical rejected payload require a fresh displayed draft.
13. Displayed candidate content comes from its stored canonical payload and digest.
    An edit creates a new identity; old approval cannot apply to the edited candidate.
    Unknown or ambiguous owner replies, agent statements, CLI calls, and unverified wrappers cannot approve or reject a candidate.
    Genuine original whole-field instructions bind the exact ID without asking the owner to type JSON, commands, or hashes.
14. Owner proof and original factual supports remain separate and are both reverified.
    The immutable candidate digest includes its disclosed transform version; derived application adds only the real owner `E_ID` as specified.
    Original support refs remain intact. Every other payload field stays unchanged, and the derived digest is retained with the receipt.
    Existing normal authority checks and erasure lineage see the actual owner evidence; no sidecar bypass substitutes for support refs.
    Pending indexing produces bounded `waiting_verification`, never fabricated proof or a false saved response.
    Deferred workers cannot apply before verified approval. Pauses, changed sources, scope, policy, or current targets block safely.
15. Checked application is idempotent and atomic across knowledge, receipt, decision outcome, and valid question resolution.
    Failure or crash cannot leave partial knowledge, a false saved status, or a false resolved question.
    Rejecting a draft leaves old withheld knowledge untouched; partial disposition or a stale replacement alone leaves the question open.
16. Session/event/text-match/transitive erasure reaches new candidate, decision, pending-work, and knowledge associations.
    Source or owner-proof erasure invalidates dependent knowledge. Erased bodies cannot be reconstructed from receipt or diagnostic copies.
    Copy, rebuild, snapshot, rollback, and both applicable fingerprint paths preserve or safely refuse the new schema as designed.
17. Exercise both decision/application race orders under one writer root.
    A verified rejection or edit supersession committed first prevents old application; application committed first returns the existing receipt.
    Late old proof cannot reopen rejected, superseded, or erased state. Duplicate approve/reject delivery returns current actual state.
    An unverified negative intent reports waiting verification; it does not falsely report cancellation.
    Failed conditional updates roll back knowledge, receipt, applied outcome, and resolution together while retaining prior decision evidence.
18. Owner intent validation rejects negated, conditional, quoted, and conflicting statements independently of a positive authority classification.
    Only a verified affirmative whole-field instruction or an equally proven reply binding can authorize the exact candidate.
19. Stage, display, and apply use the same normalization and disclose the support transformation.
    Truncated or materially incomplete display cannot be approved; changed normalization requires a fresh candidate.
20. Focused protocol, commit, recovery, hook, unresolved-view, decision, erasure, maintenance, evaluation, and concurrency tests pass in a temporary `MUNINN_HOME`.
    The final tree passes `.venv/bin/python3.13 -m tools.check --full` after the last edit.
    Report live host and cross-machine verification separately.

## Revised approval required

Conduct a fresh design review, then request one approval for this revised behavior plan and both ADR changes.
The owner delegated foreground versus background scheduling; it is not an unresolved product choice.
Exact-candidate approval, verified source binding, and truthful receipt status apply under either schedule.
The earlier approval request is superseded and does not authorize this new workflow.
