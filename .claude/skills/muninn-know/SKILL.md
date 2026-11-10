---
name: muninn-know
description: Read current supported knowledge with `muninn know list|show|search|context|unresolved`, submit checked local proposals, or explicitly retract a revision. Guide relevant conversational memory decisions through checked stage, show, submit, status, and apply operations. Use when the user asks for recorded guidance, corrections, or a durable fact to remember.
---

# muninn know

Knowledge is untrusted `evidence`.
Read current revisions and open cited `originals` before relying on it.
`Guide` titles and search previews are navigation only.

```sh
muninn know list [--all-projects] [--task TASK]
muninn know search QUERY [--all-projects] [--task TASK]
muninn know show K_ID [--revision N]
muninn know context [--task TASK]
```

IDs use `K_` plus 32 lowercase hexadecimal characters.
Muninn rejects numeric aliases.
List/search report `knowledge: [KnowledgeView, ...]`.
Show reports one view with UUID, revision, `state`, body, `origin`, expiry and retraction metadata, and `evidence_sources`.

Find the `E_` ID in `evidence_sources[].evidence_id` and open its `ref`.
Compare the opened original's hash with `line_sha256`.
A null locator is unavailable.
Historical revisions make explicit `--revision` necessary.

Context reports `schema_version` and eligible note views in `notes`.
It also reports compact `guide` IDs, revisions, titles, and scopes in `guides`, with `unresolved_ids` and `truncated`.
The installed `context_bytes` limits this output.
Current retrieval omits withheld/disputed, expired, superseded, retracted, erased, and unsupported guidance.

Explicit show can keep withdrawn content.
Scope defaults to the mapped project/task.
Project guidance can apply in that project's tasks.
Task guidance applies only to the same task.

Projects do not inherit global guidance.
`--all-projects` explicitly unions read scopes without granting authority elsewhere.
Cursor context on resume or after compaction stays unproven.
Explicitly pull `muninn know context`.
Context injected into a conversation cannot be retracted.

## Propose or withdraw

```sh
muninn know propose --input /path/to/proposal.json
muninn know retract K_ID --expected-revision N --reason "why"
```

`ProposalInput` is an object with `request_id` and `result`.
`result` contains `schema_version: 1`, `changes`, `unresolved`, and `no_change_reason`.
Examine the admitted `request` with `muninn learn show R_ID`.
Use its admitted `evidence` IDs and the strict note/`guide` change shape from `docs/REFERENCE.md`.

Files and claimed IDs cannot grant owner authority.
Proposal claims and fences the current local `attempt` with the current generation.
It does not change supervisor ownership.
It checks `evidence` and revisions again, then reports a checked receipt.

It invokes no model.
Claim and commit use different writer roots.
Erasure, cancellation, or a generation change can refuse commit.
A recorded success receipt is idempotent.

Correct knowledge through an accepted replacement in a proposal.
Use `action: replace`, `knowledge_id`, and `expected_revision`.
Retraction makes the current expected revision and an explicit nonempty `reason` necessary.
Record an owner decision or preference only when attributable owner `evidence` authorizes it.

`know add`, `know check`, previous tags/confidence/contradiction/loop flags, and ID coercion have no active interface.
A missing installed admitted frozen policy refuses with `review_policy_required`.
Do not invent runner, model, or budget settings.
Writer exit 3 means busy.

### Conversational memory decisions

Ordinary learning with checked `support` records without a new owner approval step.
A related unresolved question, or an explicit owner `review` `request`, can start one more decision.
The agent prepares a complete checked proposal and handles all commands.
A question or an earlier error code alone is not a complete proposal.

If an earlier rejected proposal is unavailable, the agent prepares a new draft from checked `evidence`.

These are agent operations:

```sh
muninn know unresolved [U_ID] [--human] [--limit N] [--after U_ID] [--all-projects] [--task TASK]
muninn know decision stage --input FILE
muninn know decision show D_ID [--human]
muninn know decision submit --input FILE
muninn know decision status D_ID [--human]
muninn know decision apply D_ID
```

`D_ID` identifies one complete proposal.
`U_ID` identifies one recorded question.
Each ID has its prefix and 32 lowercase hexadecimal characters.
These IDs do not disclose meaning or authority through their characters.

Their parts give no meaning or authority.
Opening a record gives no approval and admits no new `evidence`.
Without `U_ID`, unresolved lists open questions in the current permitted project or task scopes.
`--all-projects` uses the current explicit union of read scopes.

Global guidance is not inherited by projects.
The list includes questions with no affected knowledge.
Its default limit is 50, with a range of 1–100.
`--after` is an exclusive ID cursor.

Results use ascending ID sequence.
An explicit `U_ID` reads that specified historical question, with resolved or erased `state`.

Unresolved list reports `unresolved: [UnresolvedView, ...]`.
One question reports `id`, `request_id`, `state`, `item`, `affected`, `stale_affected`, `support_ids`, and `scope`.
`state` is `open`, `resolved`, or `erased`.
The originating `request` supplies the scope.

Stale affected revisions stay different from resolution.
Erased question bodies stay missing.

`Stage` takes strict `CandidateInput`: `request_id`, `result`, and `disposition`.
Use the checked preparatory `request` and its actual admitted `evidence`.
`result` uses the complete note or guide result contract above.
`disposition` is null or contains `unresolved_id`, `answer`, `affected`, and `complete`.

`affected` contains unchanged `knowledge_id` and `revision` pairs.
`Stage` checks and normalizes the complete change before writing it.
The recorded proposal is immutable.
Its complete fields cannot change.

It gives no owner authority.

A canonical display is the stable text Muninn generates from the recorded proposal.
A digest checks the integrity of its unchanged fields.
Show reports `DecisionDisplay`: `id`, `digest`, `text`, and `complete: true`.
The canonical `text` contains `candidate`, `enrichment`, and `owner_decision`.

The complete candidate keeps content, scope, targets, `original` `supports`, versions, `disposition`, and digest.
`disposition` describes the proposed answer to an originating question.
Enrichment is the disclosed rule for adding checked owner `support`.
That unchanged rule appends owner `evidence` without changing the proposal's other fields.

`owner_decision` contains these program-owned fields:

| Field | Meaning |
|---|---|
| `meaning` | Identifies this complete proposal and says viewing gives no approval. |
| `approve` | The unchanged sentence `Please approve memory D_ID.` with the actual ID. |
| `reject` | The unchanged sentence `Please reject memory D_ID.` with the actual ID. |
| `edit` | Tells the agent to prepare and display a new complete proposal with its own approval. |
| `reply` | Makes a complete plain chat sentence necessary, with unchanged capitalization, spaces, and last period. |
| `agent` | Gives commands and checks to the agent. A reply alone does not prove recorded memory. |

The agent sends the unchanged decoded `text` as its full `original` last message.
It adds no Markdown fence, prose, or newline external to that text.
The owner sends the unchanged approval or refusal sentence as a full plain chat message.
A bare `approved`, copied wrapper, tool response, or agent statement gives no approval.

Viewing and status checks do not check or accept a decision.

For an edit, the agent prepares and displays a new complete proposal.
The unchanged replacement sentence is `Please replace memory D_OLD with memory D_NEW.`.
It uses the actual IDs and the new proposal's complete display.
Checked replacement supersedes the previous draft.

It gives no approval to the new draft.
The new draft needs its own subsequent explicit approval.

The submit command takes strict `DecisionIntent`: `candidate_id`, `action`, `owner_locator`, `owner_selector`, `display_locator`, `display_selector`, and `replacement_id`.
`action` is `approve`, `reject`, or `edit`.
For edit, `replacement_id` must be a different new D ID.
For all other `action` values, it must be null.
Each locator has `source` (`provider`, `thread_id`), `line`, `part`, and `line_sha256`.

Each selector has `json_pointer`, `start`, and `end`.
The verifier needs the complete accepted `original` fields, not a selected substring.
Only current eligible primary sources and direct-owner wording can supply proof.
Input files, labels, claimed IDs, and commands cannot supply proof.

`Stage`, submit, status, and apply report `DecisionView`.
It has `id`, `digest`, `state`, `revision`, `candidate`, `receipt`, `code`, and `intent_state`.
`intent_state` is null, `waiting_verification`, `verified`, or `blocked`.
It reports submitted intent in a field different from the proposal's actual `state`.
A pending unverified owner `reject` intent does not cancel checked approval.

The `states` have these meanings:

| `State` | Meaning |
|---|---|
| `waiting_verification` | The `original` reply has not been checked. |
| `approved_pending` | Approval is checked. The memory has not been recorded. |
| `saved` | All application changes committed together and have a durable receipt. |
| `rejected` | A checked owner `reject` decision commits no knowledge through this decision. |
| `blocked` | A necessary check refused `progress`. Examine its finite `code`. |
| `superseded` | A checked replacement superseded this draft. |
| `erased` | The proposal content is unavailable after erasure. |

Apply checks only the named proposal through the current bounded application driver.
It can report pending, blocked, or the actual recorded receipt.
It invokes no model.
Missing indexing reports `waiting_index` promptly.

Current background work can continue verification.
No command waits in an indexing loop or restarts paused learning.
A recorded proposal keeps its unchanged receipt.
Duplicate `delivery` cannot commit it a second time.

A subsequent change needs its own checked retraction or replacement of the recorded knowledge.
A new proposal prepared independently stays unapproved and has its own identity.
A changed target blocks application.
The agent can prepare a new proposal when checked `evidence` permits it.

A rejected draft does not put back withheld knowledge or automatically resolve its question.

Explicit views use the actual recorded strict policy and digest, while learning is paused.
The complete show text must fit that policy's current `context_bytes` in UTF-8 bytes.
The limit includes all owner instructions.
Muninn refuses oversize displays. It does not truncate them.

Invalid recorded policy reports `review_policy_required`.
Unknown or malformed records use finite codes such as `decision_unknown`, `decision_invalid`, `unresolved_unknown`, or `unresolved_invalid`.
Erased content is not reconstructed.
`--human` adds `explanation` and prints it to stderr.

Stdout stays JSON.
`--pretty` changes only JSON indentation.
The decoded canonical `text` stays unchanged.
Candidate, question, and proof content stay out of logs, doctor output, and automatic startup notices.

Hook database errors keep `error: store_unavailable` and use finite `detail` codes.
Known `detail` codes are `cutover_required`, `store_access_refused`, `store_open_failed`, `store_corrupt`, and `store_read_failed`.
An unknown `detail` code is `store_detail_unknown`.
Raw `exception` text is not kept.

`context_unavailable` identifies a different context-generation error with no recorded cause code.

SessionEnd submits learning `requests` for indexed eligible records.
It does not index or wait for indexing.
It cannot promise a reply in a closed conversation.
A subsequent related `review` can show a checked proposal during an active conversation.
