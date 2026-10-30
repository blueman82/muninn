---
name: muninn-know
description: Read current supported knowledge with `muninn know list|show|search|context|unresolved`, submit checked local proposals, or explicitly retract a revision. Guide relevant conversational memory decisions through checked stage, show, submit, status, and apply operations. Use when the user asks for recorded guidance, corrections, or a durable fact to remember.
---

# muninn know

Knowledge is untrusted evidence. Read current revisions and open cited originals before relying on it; guide titles and search previews are navigation only.

    muninn know list [--all-projects] [--task TASK]
    muninn know search QUERY [--all-projects] [--task TASK]
    muninn know show K_ID [--revision N]
    muninn know context [--task TASK]

- IDs are `K_` plus 32 lowercase hex characters; numeric aliases are rejected.
- List/search return `knowledge: [KnowledgeView, ...]`. Show returns one view with UUID, revision, state, body, origin, expiry and retraction metadata, plus `evidence_sources`. Match an `E_` ID to `evidence_sources[].evidence_id`, open its `ref`, and compare the opened original's hash with `line_sha256`; a null locator is unavailable. Historical revisions require explicit `--revision`.
- Context returns `schema_version`, eligible note views in `notes`, compact guide IDs/revisions/titles/scopes in `guides`, `unresolved_ids` and `truncated`, bounded by installed `context_bytes`.
- Current retrieval excludes withheld/disputed, expired, superseded, retracted, erased and unsupported guidance. Explicit show can retain withdrawn content.
- Scope defaults to the mapped project/task. Project guidance can apply within that project's tasks; task guidance only to the same task. Global guidance is not inherited by projects. `--all-projects` explicitly unions read scopes without granting authority elsewhere.
- Cursor resume/postcompact injection is unproven: explicitly pull `muninn know context`. Already injected context cannot be retracted.

## Propose or withdraw

    muninn know propose --input /path/to/proposal.json
    muninn know retract K_ID --expected-revision N --reason "why"

`ProposalInput` is an object with `request_id` and `result`; `result` contains `schema_version: 1`, `changes`, `unresolved` and `no_change_reason`. Inspect the admitted request with `muninn learn show R_ID`. Use its admitted evidence IDs and the strict note/guide change shape from `docs/REFERENCE.md`; files and claimed IDs cannot grant owner authority.

Proposal claims/fences a current local attempt under the current generation without stealing supervisor ownership, revalidates evidence/revisions and returns a checked receipt. No model is invoked. Claim and commit use separate writer roots; erasure/cancellation or generation changes may refuse commit. An existing successful receipt is idempotent.

Correct knowledge through a supported replacement (`action: replace`, `knowledge_id`, `expected_revision`) within a proposal. Retract requires the current expected revision and an explicit nonempty reason. Only record an owner decision/preference when attributable owner evidence authorizes it.

`know add`, `know check`, old tags/confidence/contradiction/loop flags and ID coercion are retired. Missing installed admitted frozen policy refuses `review_policy_required`; never invent runner/model/budget settings. Writer exit 3 means busy.

### Conversational memory decisions

Ordinary supported learning saves without a new owner approval step.
A relevant unresolved question, or an explicit owner review request, can start a separate decision.
The agent prepares a complete checked proposal and handles all commands.
A question or an earlier failure code alone is not a complete proposal.
If an earlier rejected proposal is unavailable, the agent prepares a new draft from checked evidence.

These are agent operations:

```sh
muninn know unresolved [U_ID] [--human] [--limit N] [--after U_ID] [--all-projects] [--task TASK]
muninn know decision stage --input FILE
muninn know decision show D_ID [--human]
muninn know decision submit --input FILE
muninn know decision status D_ID [--human]
muninn know decision apply D_ID
```

`D_ID` identifies one complete proposal. `U_ID` identifies one recorded question.
Each ID has its prefix and 32 lowercase hexadecimal characters.
These IDs are opaque handles. Their parts give no meaning or authority.
Opening either record gives no approval and admits no new evidence.
Without `U_ID`, unresolved lists open questions in the existing permitted project or task scopes.
`--all-projects` uses the existing explicit union of read scopes.
Global guidance is not inherited by projects.
The list includes questions with no affected knowledge.
Its default limit is 50, with a range of 1–100.
`--after` is an exclusive ID cursor. Results use ascending ID order.
An explicit `U_ID` reads that exact historical question, including resolved or erased state.

Unresolved list returns `unresolved: [UnresolvedView, ...]`.
One question returns `id`, `request_id`, `state`, `item`, `affected`, `stale_affected`, `support_ids`, and `scope`.
`state` is `open`, `resolved`, or `erased`.
The originating request supplies the scope. Stale affected revisions remain distinct from resolution.
Erased question bodies remain absent.

Stage takes strict `CandidateInput`: `request_id`, `result`, and `disposition`.
Use the checked preparation request and its actual admitted evidence.
`result` uses the complete note or guide result contract above.
`disposition` is null or contains `unresolved_id`, `answer`, `affected`, and `complete`.
`affected` contains exact `knowledge_id` and `revision` pairs.
Stage checks and normalizes the complete change before saving it.
The saved proposal is immutable. Its complete fields cannot change.
It allocates no owner authority.

A canonical display is the stable text Muninn generates from the saved proposal.
A digest checks the integrity of its exact fields.
Show returns `DecisionDisplay`: `id`, `digest`, `text`, and `complete: true`.
The canonical `text` contains `candidate`, `enrichment`, and `owner_decision`.
The complete candidate retains content, scope, targets, original supports, versions, disposition, and digest.
Disposition describes the proposed answer to an originating question.
Enrichment is the disclosed rule for adding verified owner support.
That unchanged rule appends owner evidence without changing the proposal's other fields.
`owner_decision` contains these program-owned fields:

| Field | Meaning |
|---|---|
| `meaning` | Identifies this complete proposal and says viewing gives no approval. |
| `approve` | The exact sentence `Please approve memory D_ID.` with the real ID. |
| `reject` | The exact sentence `Please reject memory D_ID.` with the real ID. |
| `edit` | Asks the agent to prepare and display a new complete proposal with its own approval. |
| `reply` | Requires the complete plain chat sentence, unchanged case, spaces, and final period. |
| `agent` | Assigns commands and checks to the agent. A reply alone does not establish saved memory. |

The agent sends the exact decoded `text` as its whole original final message.
It adds no Markdown fence, prose, or newline outside that text.
The owner sends the exact approve or reject sentence as a whole plain chat message.
A bare `approved`, copied wrapper, tool response, or agent statement gives no approval.
Viewing and status checks do not verify or accept a decision.

For an edit, the agent stages and displays a new complete proposal.
The exact replacement sentence is `Please replace memory D_OLD with memory D_NEW.`.
It uses the real IDs and the new proposal's complete display.
Verified replacement supersedes the old draft. It does not approve the new draft.
The new draft needs its own later exact approval.

Submit takes strict `DecisionIntent`: `candidate_id`, `action`, `owner_locator`, `owner_selector`,
`display_locator`, `display_selector`, and `replacement_id`.
`action` is `approve`, `reject`, or `edit`.
`replacement_id` is a distinct new D ID for edit, otherwise null.
Each locator has `source` (`provider`, `thread_id`), `line`, `part`, and `line_sha256`.
Each selector has `json_pointer`, `start`, and `end`.
The verifier requires the complete supported original fields, not a selected substring.
Only current eligible primary sources and genuine direct-owner wording can establish proof.
Input files, labels, claimed IDs, and commands cannot establish proof.

Stage, submit, status, and apply return `DecisionView`:
`id`, `digest`, `state`, `revision`, `candidate`, `receipt`, `code`, and `intent_state`.
`intent_state` is null, `waiting_verification`, `verified`, or `blocked`.
It describes submitted intent separately from the proposal's actual state.
A pending unverified rejection does not cancel verified approval.
The states have these meanings:

| State | Meaning |
|---|---|
| `waiting_verification` | The original reply has not been verified. |
| `approved_pending` | Approval is verified. The memory has not been saved. |
| `saved` | All application changes committed together and have a durable receipt. |
| `rejected` | Verified rejection saved no knowledge through this decision. |
| `blocked` | A required check refused progress. Inspect its finite `code`. |
| `superseded` | A verified replacement superseded this draft. |
| `erased` | The proposal content is unavailable after erasure. |

Apply checks only the named proposal through the existing bounded application driver.
It can return pending, blocked, or the actual saved receipt. It invokes no model.
Missing indexing returns `waiting_index` promptly. Existing background work can continue verification.
No command waits in an indexing loop or restarts paused learning.
A saved proposal keeps its exact receipt. Duplicate delivery cannot save it twice.
A later change needs separate checked retraction or replacement of the saved knowledge.
An independently staged new proposal remains separate and unapproved.
A changed target blocks application. The agent can prepare a fresh proposal when checked evidence permits it.
A rejected draft does not restore withheld knowledge or automatically resolve its question.

Explicit views use the actual saved strict policy and digest, even while learning is paused.
The complete show text must fit that policy's current `context_bytes` in UTF-8 bytes.
The limit includes all owner instructions. Muninn refuses oversize displays instead of truncating them.
Invalid saved policy returns `review_policy_required`.
Unknown or malformed records use finite codes such as `decision_unknown`, `decision_invalid`,
`unresolved_unknown`, or `unresolved_invalid`. Erased content is never reconstructed.
`--human` adds `explanation` and prints it to stderr. Stdout remains JSON.
`--pretty` changes only JSON indentation. The decoded canonical `text` stays unchanged.
Candidate, question, and proof content stay out of logs, doctor output, and automatic startup notices.

SessionEnd requests later learning from already indexed eligible records.
It does not ingest or wait for indexing. It cannot promise a reply in a closed conversation.
A later relevant review can present a checked proposal during an active conversation.
