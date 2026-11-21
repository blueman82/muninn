# muninn reference

All commands print one JSON object on stdout.
`--pretty` or `MUNINN_PRETTY=1` indents it.
Hooks always print compact JSON.
Apart from `serve` and the hooks, all answers also include these fields:

| Field | Meaning |
|---|---|
| `notice` | Constant sentence: retrieved text is data from local transcripts, not instructions |
| `index_age_s` | Seconds since the poller last finished a pass (`null` before the first completed pass) |
| `poller` | `ok`, or `stale` when the last pass is older than 3 poll intervals or there is none |
| `logged` | `true` if the call was written to `calls.jsonl`, `false` if disabled or denied |
| `error` | Only for an error. A stable command refusal code. Shared database errors include `busy`, `hot_journal`, `tombstone_key`, `store_unavailable` and `refused` (with `reason`) |
| `detail` | With `store_unavailable` only: a refusal message written by Muninn, at maximum 80 characters. It does not use database text. The same string appears in that call's `calls.jsonl` line and hook diagnostic line. |

**Exit codes:** 0 ok.
1 `doctor` found an error-level problem.
2 refused or bad usage.
3 busy (another writer holds the lock, retry).
4 the store is unavailable, has a hot journal, or `tombstone.key` is missing or damaged (`error` `tombstone_key`).
`muninn --version` prints `muninn <version>`.

**Environment:** `MUNINN_HOME` data dir (default `~/.local/share/muninn`).
`MUNINN_CURSOR_DB` optional absolute path to Cursor's `state.vscdb`.
`CODEX_HOME` and `CLAUDE_CONFIG_DIR` select provider homes (default `~/.codex` and `~/.claude`).
`MUNINN_ROOTS` JSON map of provider root name to path (tests).
`MUNINN_PYTHON` interpreter for `bin/muninn`.
`MUNINN_PRETTY=1` indent.
`MUNINN_NO_CALLLOG=1` no `calls.jsonl` line.
`MUNINN_HOOK_DISABLE=1` hooks print `{}`.
`CLAUDE_CODE_SESSION_ID`, `CODEX_SESSION_ID`, `CODEX_THREAD_ID` identify the calling session, which search leaves out unless told not to.

The managed poller and learning run on macOS only (ADR 0026).

## Reading memory

| Command | What it does | Primary fields in the answer |
|---|---|---|
| `muninn search QUERY` | Ranked knowledge and `events` for this repo. Worktrees use the primary repo scope. | `hits[]`, `knowledge[]` (current typed previews), `has_more`, `page`, `limit`, `stages`, `scope`, `other_scopes` |
| `muninn open REF` | One `event` in full with neighbors. REF is an `event` id or `provider:thread_id:line.part` | `text`, `neighbours[]`, `provenance`, `hash_ok`, `redacted`, `truncated`, `next_offset` |
| `muninn sessions` | Sessions in scope, newest first | `sessions[]` (`session`, `provider`, `events`, `threads`, `forks`, `first_ts`, `last_ts`, `kinds`, `preview`) |
| `muninn session ROOT` | One session's `events` across its threads, in sequence. ROOT is the `session` value from `sessions`. | `events[]` includes `id`, `ref`, `kind`, `role`, `ts`, `preview`, `answer_citable`, and `tag`. The answer also has `total`, `provider`, and `next_from`. Use `next_from` as `--from` for the next page. |
| `muninn quote-check REF QUOTE` | Checks if QUOTE appears verbatim in that `event`. | The equality-check result. |

**`search` flags:** `--all-projects` includes other repos.
`--include-subagents`.
`--include-current` also examines the caller's own session.
`--current-session ID` name it.

`--kind a,b` `event` types.
`--provider codex|claude|cursor`.
`--scope DIR` limits `events` to the same cwd as DIR.
`--since`/`--until`.

`--session ID`.
`--recent` newest first.
`--limit N` limits the number of hits.
The number on each page varies with its byte budget.
When `has_more` is `true`, use `--page N` with N+1.
Continue after an empty page if `has_more` is `true`.

**`open` flags:** `--context N` includes neighbors on each side, with default 3.
`--offset N` continue with `next_offset`.
`--raw` the raw JSONL line with `line_sha256`.
**sessions/session flags:** `--all-projects`, `--since`, `--limit`.

`session --from N` continue with `next_from`.

For what the owner said, specified or decided, start with `--kind prompt`.
For an assistant's report, use `--kind reply`.
Add `--provider` when known.
If that pass misses, widen the types and open the adjacent conversation.

An assistant's report alone does not prove an owner instruction.
Check authorship after opening: prompt `events` can contain copied text or local command output.

**Hit fields:** `ref` stable source locator.
`id`.
`kind`.
`role` user|assistant.

`provider`.
`session`.
`thread`.
`cwd`.

`scope`.
`ts`.
`snippet` (navigation only).
`answer_citable` (`false` means it cannot be a source for a factual answer).

`source_status` active|missing.
`tag`.
`repeats` and `more_in_session` count collapsed near-duplicates.
**`stages`:** `matches` each eligible match with other repos.

`in_scope` those in this repo's scope.
`candidates` the ranked rows fetched for this page (capped).
`session_capped` and `tool_capped` dropped by the per-session and tool-call caps.
`returned` hits on this page.

**Knowledge preview fields:** `id` (`K_` UUID), `revision`, `state`, `form`, `kind`, `scope`, `text` and `evidence_refs`.
These are current eligible previews, not complete guidance.
Open `muninn know show K_ID` to check the current revision and cited `evidence` before relying on it.
A historical `review` can select its recorded revision with `--revision N`.

**`event` types:**

| Type | Meaning |
|---|---|
| `prompt` | A user message, with messages the owner typed during Claude work. |
| `reply` | An assistant message. |
| `tool_call` | A tool invocation. Default queries exclude it, and it cannot be a cited source. |
| `harness` | Provider-injected text. |
| `delegation` | A parent message to a subagent thread. |
| `tool_error` | The redacted head and tail of tool output that has an error. It is not pushed and cannot be a cited source. |

Muninn indexes queued input only with `origin` equal to `human`.
It skips other `origin` values: task, peer, coordinator, auto-continuation, or none.
It also skips chat-channel participants.
A message flagged as meta is `harness`.

In a subagent thread, that message follows the usual subagent rule and is `delegation`.
The thread classes are `primary`, `subagent`, `reviewer`, and `other`.
Default queries include `primary`.

`primary` includes a Codex desktop session that continues a ChatGPT conversation, with `thread_source` `chatgpt_handoff`.
Muninn records its opening message, copied conversation, and ChatGPT-composed `request` as `harness`.
`other` means an unrecognized format. Muninn does not guess that it is primary.

**Providers:** `claude`, `codex`, `cursor`.
**Source roots:** `claude-projects`, `codex-sessions`, `codex-archived`, `cursor-imports`.

**Retrieval outcome counts.**
`stages` in a `search` or `hook prompt` line of `calls.jsonl` can carry integer flags that appear only with value 1.
`miss_zero` means searchable terms but no result.
`miss_outside` means results exist but none are in scope (it depends on the working directory; `--all-projects` could find them).
`miss_noterms` (search only) means no searchable terms, and `stages` then has only this key.
`miss_floor` (hook only) means the term floor dropped all events read.
The hook's `stages` also has `floor_dropped`, `returned` and `pool_error`; with `pool_error` set, `miss_floor` is left out.
A flag is set only when the call found no hit and no knowledge, and never when it had an error.
The hook skips prompts with fewer than 3 query terms or a first character `/`, and logs no `stages` for them.
The log has `n_terms`, but no query hash or text; `MUNINN_NO_CALLLOG=1` stops it.
These flags describe one call and do not prove that no related answer exists.

## Current knowledge and learning

Knowledge IDs are `K_` plus 32 lowercase hexadecimal characters.
`request` IDs use `R_` with the same shape.
Muninn rejects numeric knowledge aliases.
List/search report `knowledge: [KnowledgeView, ...]`.

A single typed view keeps its fields at this JSON object's root, with the same notice, freshness, and logging metadata.

| Command | Meaning/output |
|---|---|
| `muninn know list [--all-projects] [--task TASK]` | Current eligible `KnowledgeView` objects, newest first, and `proofs`: full owner proofs by knowledge ID. |
| `muninn know search QUERY [--all-projects] [--task TASK]` | SQLite FTS5 search of current permitted knowledge. No embeddings. |
| `muninn know show K_ID [--revision N]` | `KnowledgeView` fields plus `evidence_sources` and `proof` (the full owner proofs). This maps each `E_` ID to the transcript `ref` and `line_sha256` for that revision. Unavailable sources have null locators. Historical revisions need `--revision`. Explicit show can keep withdrawn content. |
| `muninn know show --proof K_ID [--revision N]` | Only `id`, `revision` and `proof` (the full owner proofs), with no model text. Agents use it for a proof too long for their context. |
| `muninn know context [--task TASK]` | `KnowledgeContext`: `schema_version`, complete note views in `notes`, compact `guides` (`id`, `revision`, `title`, `scope`), `unresolved_ids`, `truncated`. Bound by installed policy's `context_bytes`. |
| `muninn know context --page N [--provider claude\|codex\|cursor]` | Plain text, not JSON: page N of the session-start memories in the same untrusted-data frame and layout as the start block, with control characters replaced. The header says `page N · X of Y`, and `page N of N · X of Y` on the last page. Page 1 is the start block's list. Each page continues where the last one stopped, so each memory appears once. `--provider` (default `claude`) sets the page size: Claude and Cursor 4,000 bytes, Codex 2,800. `--page` with `--task` reports `page_task_unsupported`; `--provider` without `--page` reports `provider_needs_page`; page 0 or a missing page reports `page_unavailable`. |
| `muninn know propose --input FILE` | Strict `ProposalInput`, then a checked current local `attempt` and `Receipt`. No model invocation. |
| `muninn know retract K_ID --expected-revision N --reason TEXT` | Checked withdrawal of that current revision. Reports `KnowledgeView`. |
| `muninn learn request --session REF [--task TASK]` | `RequestView` for one bounded `request` from the activation or uncompleted boundary through the latest complete record. `--task` binds it to one task in the source project. |
| `muninn learn on` | Turn learning on. Saves `learning.json` and learns only chat from now on. Output `{"enabled":true}`. |
| `muninn learn off` | Turn learning off and stop new reviews. Output `{"enabled":false}`. |
| `muninn learn list [--state STATE]` | `requests: [RequestView, ...]`. `state` is `pending`, `running`, `waiting`, `completed`, `failed` or `cancelled`. |
| `muninn learn show R_ID` | `ReviewDetail`: `request`, `attempts`, durable calls, governed `evidence`, receipt/null, `correction_status`, and safe `diagnostics`. Content is shown only when specified, not logged. |
| `muninn learn explain R_ID [--human]` | Gives `request_id`, `correction_status`, and `explanation` as JSON. The explanation covers kept history, missing information, recorded changes, current admission and the failure code. `--human` also prints it to stderr. Stdout stays JSON. Muninn displays no transcript or proposal text. |
| `muninn learn retry R_ID` | Explicit restart of the same `request`, with cancelled `requests` when current guards permit. Completed `requests` keep their receipt/`state` without a different commit. |
`REF` for learning is an indexed `provider:thread_id` or an unambiguous session root.
`Request` creation is admission, not completed `review`.
Missing or invalid installed frozen policy refuses with `review_policy_required`.


`ProposalInput` contains only `request_id` and `result`.
`result` contains `schema_version: 1`, `changes`, `unresolved` and `no_change_reason`.
Use `evidence` IDs from this request's admitted information.
IDs, claims and local files confer no owner authority.

Proposal claims and fences the request's current `attempt` without taking supervisor ownership.
It checks physical `evidence`, revision, and generation again before atomic commit.
Claim and commit use different writer roots: intervening erasure or cancellation can refuse the commit.
A correct receipt records `request_id`, `attempt_id`, `applied` revision pairs, `unresolved_ids`, `committed_at`, `outcome` and nullable `no_change_reason`.
Current success receipts stay idempotent.

Human correction uses an accepted proposed replacement with `knowledge_id` and `expected_revision`.
Only explicit retract does withdrawal without replacement.

A note change needs `action: add`, `form: note`, `kind`, `scope` (`project_id`, `task`), `content`, and `conditions` (a list).
Nonempty `evidence_refs` and `reason` are also necessary.
A guide change uses `form: guide`, `title`, `summary`, `applicability`, `steps`, `decision_points`, `pitfalls` plus the same type/scope/`reason`.
Each accepted claim has `text` and `evidence_refs`.
Each guide step has `key`, `claim`, and `depends_on`.
Dependencies can refer only to steps before that step.

A preference, a decision, a global change, or a replacement of a preference or decision needs owner support (ADR 0022).
The model cites evidence by ID only. Muninn does not compare the claim text with the evidence.
Owner support needs a cited `direct_owner` owner message with a full-field selector (`start` and `end` null).
A partial span gives no owner support.
For agreement, the claim also cites the final assistant reply directly before the owner reply.
Muninn reads the gap again once for each pair; a tool call, tool result, or other message between them refuses the batch with `rejected_authority`.
Owner authority comes from provenance (provider prompt label, an indexed `prompt` event, no injection flag, no tool result, no harness tag, and no Muninn marker), not from wording.

The proof is the full cited owner message, plus the paired assistant reply for agreement, as Muninn recorded them.
`know context`, `know search`, recall, and review model input show the proof in place of the model's claim text for preferences, decisions, and global knowledge.
A proof over 2,000 bytes is not cut: the agent gets `proof too long: run muninn know show --proof K_ID`.
A search preview or recall line that has less room than the proof also gets that line, never part of the proof; such a preview has `proof: true`.
If the proof cannot be found, the agent gets `proof unavailable: run muninn know show --proof K_ID`, never the model text.
A hidden reason, guide title or expiry condition reads `not shown to agents: run muninn know show --proof K_ID`.
`muninn search` knowledge previews follow the same rule.
Knowledge saved through an ADR 0018 owner approval shows the model text the owner approved.
Facts and procedures show the model text.
`know list` (`proofs`, by knowledge ID), `know show` (`proof`), and `know decision show` and `status` (`proof`) show the full proof beside the model text.

A replacement uses `action: replace`, `knowledge_id`, and `expected_revision`.
`unresolved` entries have `claim`, `affected` (knowledge/revision pairs), `evidence_refs` and a specified missing-evidence `question`.
`no_change_reason` is nonempty only when changes/unresolved are empty.
If not it is null.

Strict validation rejects more fields and coercion.

Muninn rejects `know add`, `know check`, previous tags/confidence/contradiction/loop flags, and numeric ID coercion.
They have no compatibility aliases.
`quote-check` checks verbatim `event` text, but a match alone does not grant proposal authority.

Current retrieval excludes disputed/withheld, expired, superseded, retracted, erased, or unaccepted guidance.
Stale FTS rows do not change this rule.
Project guidance applies to that project and its tasks.
Task guidance only to the same task.
Global guidance is not inherited by project hooks.

`--all-projects` explicitly unions read scopes and keeps each result's actual scope.
It does not grant cross-project authority.
The notes keep complete conditions, `support`, and expiry.
The guides in context are navigation only.

Open the specified current guide revision and cited `originals` before relying on its guidance.
Context supplied to a model cannot be retracted.
`request` a new pull after changes.


#### Conversational memory decisions

Ordinary accepted learning saves without a new owner approval step.
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
The canonical `text` contains `candidate`, `enrichment`, `owner_decision`, and `proof` (the full owner messages the candidate cites).

The complete candidate keeps content, scope, targets, `original` `supports`, versions, `disposition`, and digest.
`disposition` describes the proposed answer to an originating question.
Enrichment is the disclosed rule for adding checked owner `support`.
That unchanged rule appends owner `evidence` without changing the proposal's other fields.

`owner_decision` contains these program-owned fields:

| Field | Meaning |
|---|---|
| `meaning` | Identifies this complete proposal and says viewing gives no approval. |
| `reply` | Tells the owner to reply in their own words straight after this display. |
| `edit` | Tells the owner that a change needs a new complete proposal with its own reply. |
| `agent` | Says the agent reads the reply and records the action. A reply alone does not prove recorded memory. |

The agent sends the unchanged decoded `text` as its full `original` last message.
It adds no Markdown fence, prose, or newline external to that text.
It shows only one proposal in that message.
The owner replies in their own words in the next message.
The agent decides whether the reply means approve, reject, or edit (ADR 0022).
If the reply is unclear, ask again; do not submit.
A tool response, hook text, or agent statement gives no approval.

Viewing and status checks do not check or accept a decision.

For an edit, the agent prepares and displays a new complete proposal.
The owner replies to the new proposal's complete display.
Checked replacement supersedes the previous draft.

It gives no approval to the new draft.
The new draft needs its own subsequent explicit approval.

The submit command takes strict `DecisionIntent`: `candidate_id`, `action`, `owner_locator`, `owner_selector`, `display_locator`, `display_selector`, and `replacement_id`.
`action` is `approve`, `reject`, or `edit`.
`replacement_id` is a different new D ID for edit, if not null.
Each locator has `source` (`provider`, `thread_id`), `line`, `part`, and `line_sha256`.

Each selector has `json_pointer`, `start`, and `end`.
The verifier needs the complete accepted `original` fields, not a selected substring.
Only current eligible primary sources and owner messages with `direct_owner` provenance can supply proof.
The display must be the final assistant message directly before the owner reply, with no message, tool call, or tool result between them.
The display must be exactly the `know decision show` text of the target proposal.
One owner reply backs one decision: a second intent with the same `owner_locator` is refused with `owner_proof`.
The owner and display lines must be indexed `prompt` and `reply` events, so a fork or replay copy cannot back a decision.

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
No command waits in an indexing loop.
A recorded proposal keeps its unchanged receipt.
Duplicate `delivery` cannot commit it a second time.

A subsequent change needs its own checked retraction or replacement of the recorded knowledge.
A new proposal prepared independently stays unapproved and has its own identity.
A changed target blocks application.
The agent can prepare a new proposal when checked `evidence` permits it.

A rejected draft does not put back withheld knowledge or automatically resolve its question.

Explicit views use the constant review policy.
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


### Learning failure messages

Each failed request keeps one finite `error_code`.
Muninn explains it with one fixed sentence and never repeats stored text.
`muninn learn show R_ID` adds `diagnostics`; `muninn learn explain R_ID [--human]` prints the explanation (to stderr with `--human`; stdout stays JSON).
A failed request stays in `learn list --state failed` and keeps its receipt, attempts and calls.
A rejected batch commits no part of its knowledge changes.

Evidence selection and window-boundary verification use one physical source scan, then Muninn rereads the selected record before admission.
Each hook or catch-up window gets its own read budget of `max_input_bytes`; the limit includes source traversal, not only selected text (ADR 0026).
Before admission, Muninn cuts a Codex or Claude window at a line until it holds at most 200 lines and the safe limit of original bytes.
It skips the window when one line alone passes the limit, and writes one `review_skipped` log record with `error_code` and `skipped_lines`.
A later read over its allowance fails the request with `evidence_limit`.

| Code | Meaning and response |
|---|---|
| `deadline` | The review exceeded its time limit. Muninn retries within the attempt limit. |
| `unavailable` | The model program did not finish. Muninn retries within the attempt limit. |
| `auth` | The model program is not signed in. Sign in, then run `muninn learn retry R_ID`. |
| `budget` | The review budget was used up. |
| `config` | Muninn has no safe cause for this failure. Run `muninn doctor --pretty`. |
| `source_unavailable` | The source transcript is no longer an indexed active primary source. Index it again. Do not retry unchanged. |
| `review_policy_required` | The review policy changed while the request waited. Do not retry unchanged. |
| `incomplete_window` | The indexed source no longer holds the whole admitted window. Index it again or wait for the next window. |
| `budget_clock_reversed` | The system clock moved backwards. Check the clock, then run `muninn doctor --pretty`. |
| `prompt_limit` | The complete prompt exceeded its input byte limit. Do not retry unchanged. |
| `evidence_limit` | Original-source verification exceeded a read limit. Do not retry unchanged. |
| `evidence_byte_limit`, `evidence_record_limit`, `evidence_work_limit`, `evidence_selection_limit` | The limit that was hit: physical bytes, physical records, database work, or selected content, in that order. For JSONL sources, lines before the window start do not count. Cursor sources count from the first record. |
| `rejected_authority` | The proposed change has no checked owner evidence. Muninn rejects it and continues. |
| `rejected_revision` | Saved knowledge changed during the review. Muninn rebuilds the review within the attempt limit. |
| `rejected_validation` | The result does not match the required format. Muninn rejects it and continues. |
| `rejected_evidence` | Source evidence is missing, changed, or cannot be checked. |
| `rejected_scope` | The change is outside the admitted project scope. |
| `rejected_erased` | An erase record excludes the evidence. |
| `rejected_fenced` | Another worker took over the request. Muninn excludes this result. |

`correction_status` describes the latest attempt with a durable correction marker: `requested`, `attempted`, `succeeded`, `exhausted` or `unknown`.
Only the commit receipt proves recorded changes.

## Learning settings

`muninn learn on` and `muninn learn off` write `learning.json` in the data directory (mode 0600) and print `{"enabled":true}` or `{"enabled":false}`.
The poller (`muninn serve`) is the only learner.
The file has four values:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Whether the poller starts reviews. |
| `provider` | `claude` | `claude` runs the Claude CLI. `codex` runs the Codex CLI. |
| `model` | `sonnet` | Model name passed to the provider. |
| `effort` | `low` | Effort passed to the provider. |

An unreadable or invalid file counts as off.
The review policy is one constant: 2 rounds, 1 MiB input, 32 KiB output, 12,000 context bytes, a 120 second time limit for each call, 3 attempts with waits of 5 s and 30 s, one worker.
`deadline` is that time limit.
Each call is one non-interactive provider command with fixed options and a read-only result schema.

## Running and maintaining

| Command | What it does |
|---|---|
| `muninn ingest [--full]` | Catch up with Claude and Codex transcripts now. `--full` rescans everything and imports Cursor history from `MUNINN_CURSOR_DB`, or `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`. Cursor data is read-only and not polled. Answer: `files_seen`, `files_changed`, `events_added`, `events_removed`, `skipped_files`, `unreadable_files`, `skipped_lines`, `failed`, `errors`, `missing`, `duration_s` |
| `muninn serve [--interval S]` | The managed poller loop (default 60 s). Prints nothing. It is also the only learner. |
| `muninn stats [--usage]` | Counts, see below |
| `muninn doctor` | Health checks, see below. Exit 1 if an error-level check is `false`. |
| `muninn compact` | VACUUM the database with the writer lock held. Answer `compact.bytes_before`/`bytes_after`, `db_space`. Refuses without about one database of free disk |
| `muninn rebuild` | Rebuilds the store from the transcripts and keeps the old file as `muninn.sqlite.recovery-<id>`. It copies each governed table, keeps Cursor and missing-source history, re-ingests all transcripts and replays erase intent. |
| `muninn erase` | Forgets selected events and the knowledge, evidence, requests and questions that rest on them. Without `--yes` it is a dry run. See below. |
| `muninn recall-review [--batch N] [--ack]` | Reviews a batch of 50 recall deliveries, see below. |
| `muninn hook ...` | Provider hooks: payload on stdin, compact JSON on stdout, exit 0 always. |

```text
muninn erase --session S | --event REF | --match TEXT [--dry-run] [--yes]
muninn hook session-start|prompt|session-end|pre-compact --provider claude|codex|cursor
```

`rebuild` answer: `rebuilt` and `old_readable` are `true` on success, `old_kept_as` names the archive, `copied` has table counts, `reapplied_tombstones` counts replayed erase intent, `ingest` has the totals below.
A concurrent writer or a source that changed during the run refuses with `source_changed` (exit 2).
A failed replacement exits 4 with `replace_failed`.
Keep the live file, `tombstone.key`, `tombstones.jsonl` and every archive after any error.

Index counters describe work across the whole invocation.
`files_seen`, `skipped_files` and `unreadable_files` count visits across passes, not distinct files.
`files_changed`, `events_added`, `events_removed` and `skipped_lines` sum committed work.
`failed` and `errors` include failed read attempts even when a later attempt succeeded.
Each pass takes the writer lock on its own; Git lookups run while it is free, each with a ten second timeout.
The poller publishes its heartbeat and clears its last error only after a pass that completes without an error.

## Installing and removing (from a checkout)

| Command | What it does |
|---|---|
| `bin/muninn-install` | Installs a new release or upgrades the installed one at HEAD, restarts the poller and runs doctor. |
| `bin/muninn-install --check` | Preview in plain sentences. Writes no files. |
| `bin/muninn-install --status` | Compares the installed commit with HEAD. |
| `bin/muninn-uninstall [--dry-run] [--purge-data]` | Same as `bin/muninn-install --uninstall`. |

Other arguments go to `install.installer`: `--repo`, `--sha`, `--fresh`, `--upgrade`, `--dry-run`, `--home`.

A new install finishes transcript indexing (and Cursor history when available) before it activates the release.
Recall starts off (`recall.off`), and learning starts off.
`--upgrade` keeps the mode files and `learning.json`.
Before an upgrade replaces a store that needs a new format, the installer keeps a private copy named `muninn.sqlite.pre-upgrade-<sha>` (mode 0600).
Each `FAILED: <code>` line is followed by `What happened:`, `Why:` and `What to do:`, taken from a fixed table that prints no path, file content or exception text.
After an upgrade the previous release is erased, and there is no way back to it (ADR 0001).

The uninstall command stops the poller and removes the launchd plist.
It removes Muninn hooks from Claude's `settings.json` and Cursor's `hooks.json`, and Muninn sections from Codex's `config.toml` and plugin cache.
It removes `~/.local/lib/muninn`, and `~/.local/bin/muninn` only when the link points into that directory.
It moves the data directory to `~/.local/share/muninn-removed-<ts>/muninn`; `--purge-data` removes it instead.
`--dry-run` writes nothing.
It prints one line per step and ends with `uninstall done`, `dry run only: nothing was removed`, or `muninn is not installed here` (exit 0).
A configuration it cannot safely edit prints `FAILED: <why>` (exit 1).

## `muninn erase`

Select one session, one event locator, or a literal match of at least four characters after whitespace is trimmed.
`--match` searches event text and governed knowledge content, conditions, claims, evidence excerpts and unresolved content.
Erasure follows support and review-read links through earlier revisions, withdraws affected guides, clears explanations and fences affected requests.
Candidates, decision revisions and pending decision locators are cleared too, so an erased candidate cannot be shown or applied.
IDs, statuses, digests and timestamps stay for audit.
Removed text never reaches the call log.

Erasure takes the writer lock; a poller pass in progress makes it exit 3 (busy).
A missing database exits 4 with `store_unavailable` and creates nothing.
A dry run writes no `tombstone.key`.
Tombstones are written before any content is scrubbed and are replayed first on the next start.
Event content tombstones are keyed HMAC-SHA256 tags (`ev2:`) made with `tombstone.key`; they apply to fork replay.
Provider transcripts and kept archives stay unchanged, and the answer lists kept archives as `residue` and in `aside_files`, with the literal removal command in `aside_remove`.
`vocab` and `residue` report bounded checks, not proof that all text is gone, and nothing about copies held by providers.
Exit codes: busy 3, hot journal, `tombstone_key` or unavailable store 4, bad arguments 2.

## `muninn stats`

| Field | Meaning |
|---|---|
| `sources` | Counts for each `provider/root/thread_class/status`, with status `active` or `missing`. |
| `events` | Counts by `event` type. |
| `events_by_provider` | Counts by provider. |
| `flags` | `marker` means text looked like an injected block. `redacted` means a secret was removed. `truncated` means above 64 KiB. |
| `skipped_lines` and `issues` | Lines that were not parsed: `line_too_large`, `invalid_json`, `too_deep`, or `not_object`. |
| `other_threads` | Unrecognized threads by `reason`. |
| `knowledge` | Counts by durable `state`: `active`, `withheld`, `retracted`, or `erased`. |
| `evidence` | Counts by `state`: `live`, `changed`, or `erased`. |
| `tombstones` | Counts by level: `session`, `thread`, or `line`. |
| `db_bytes` | File byte count. |
| `db_space` | `page_count`, `freelist_count`, `page_size`, and `free_ratio`. |
| `install_sha` | Installed commit identity. |
| `classifier_version` | Current classifier version. |
| `hash_mismatches` | Open-time line hash errors seen in the call log. |

`last_pass` has `last_pass_at`, `duration_s`, `files_changed`, `events_added`, `skipped_files`, `unreadable_files`, and `last_error`.
It also has `failed`, `errors`, `busy_skips`, `index_age_s`, `poller`, and `alive_age_s`.
`last_error` is the class name from the last pass with an error.
The poller's next good pass makes it null.

`alive_age_s` counts seconds since the last alive timestamp during an active pass.
It is null between passes or without a usable timestamp.
It increases if the poller stops during a pass and does not start again.

`reread` has `pending` and `of`.
`reread.pending` counts active sources read with an earlier classifier version.
`reread.of` counts all active sources.
They show the one-time re-read after a classifier change.
The pending count usually falls to 0.

A source with an unsuccessful or skipped read stays pending.
Examine `last_pass.failed`, `unreadable_files`, and `skipped_files` as invocation `attempt` totals, not counts of unresolved sources.
Those totals can include sources repaired subsequently.

`--usage` adds `usage` for each session: `calls`, `errors`, and `last_ts` for Muninn calls seen in transcripts.
It also adds `usage_totals` for each provider.
This is different from measured worker `usage` below.

`stats` always reports `retrieval_outcomes` from the same call-log window.
It has `window_lines`.
`search` has `calls`, `zero`, `outside`, and `noterms`.
`hook_prompt` has `calls`, `zero`, `outside`, and `floor`.

The calls without `stages` count in `calls` only.
These counters are facts, not proof of a miss.

`review` is a schema-1 `ReviewStats` object with content-free counts.
`admission.enabled` is `true` when learning is on.
`requests_by_state` and `calls_by_state` count durable rows.
`oldest_pending_age_seconds` covers pending and waiting requests, and `last_progress_age_seconds` the newest attempt progress; both are nonnegative or null.
`eligible_records` and `completed_records` count indexed prompt and reply records, deduplicated by provider, thread and line.
Only success receipts (including no-change receipts) with current indexed provenance count as completed.
`unresolved_items` counts open items, and `usage` sums reported per-call usage; unknown parts stay null.
`observed_enqueue_busy` counts kept `review_enqueue_busy` log records, and `busy_count_complete` is always `false`.
Stats read no transcript, so these are indexed observations, not live validation.

## `muninn doctor`

Answer: `ok` (`true` when no error-level check is `false`) and `checks[]`.
Each check has four fields:

| Field | Meaning |
|---|---|
| `check` | The check's name. |
| `ok` | `true` means the check passed. `false` means it was unsuccessful. `null` means it could not be checked. |
| `level` | `error`, `warn`, `info`, or null. An error-level `false` makes the full answer `ok: false` and exit 1. Warnings and information do not make the run unsuccessful. A null level means unverified. |
| `detail` | A short string with the meaning given below. Empty when there is nothing to add. |

A null `launchd_job` result does not prove that the service runs.
It reports constant codes when observation fails: `launchctl_unavailable`, `launchctl_timeout`, `launchctl_failed`, `launchctl_pid_unavailable`, `process_inspection_unavailable`, `process_inspection_timeout`, `process_inspection_failed` and `process_pid_unverified`.
Each native command has a 60 second limit, and no native message or command line is reported.
Exit 0 means only that no error-level check is `false`.
If doctor reports a stopped service, start it with the system service manager.
Doctor never stops a service or changes data files.

| Check | Level | Satisfactory when | `detail` |
|---|---|---|---|
| `data_dir_mode` | Error | The data dir is mode 0700 | Its mode, or `absent` |
| `file_modes` | Error | No data file is readable by group or others | Names of loose files |
| `unexpected_files` | Error | Only known files are in the data dir | Names of stray files |
| `unowned_journal` | Error | No crashed writer's journal is lying around | Explanation |
| `store_readable` | Error | The database opens read-only and has the right schema | The refusal message if not, such as `store format marker missing` or `cannot read the store (SQLITE_BUSY)` |
| `journal_mode` | Error | SQLite journal mode is `delete` | The mode |
| `fts_secure_delete` | Error | The two full-text indexes have `secure_delete` on. | None. |
| `quick_check` | Error | SQLite's `quick_check` says `ok` | Its first 80 characters |
| `missing_sources` | Info | Always | Count of indexed files no longer on disk |
| `other_threads` | Info | Always | Count of unrecognized thread types |
| `reread` | Info | Always | `N of M`: active sources to be re-read after a classifier change. Examine `failed_sources` and `stats` `last_pass.unreadable_files` as `attempt` totals, not different or unresolved source counts |
| `db_size` | `warn` | The database is below 2 GiB. | Its byte count and the threshold. |
| `db_free_space` | `warn` | Free pages are below 25% or below 64 MiB. | Free byte count and ratio. `run: muninn compact` |
| `writer_secure_delete` | Error | Writer connections turn `secure_delete` on. | None. |
| `heartbeat` | Error | The poller finished a pass, or an active pass stamped `alive_at`, in 3 intervals (`poller: ok`) | `index_age_s` |
| `failed_sources` | `warn` | No ordinary source read `attempt` had an error during the last completed invocation. | Unsuccessful read `attempts`, with duplicate `attempts` and possibly subsequently repaired sources. Missing Git lookup facts do not count. The SessionStart block, and a recall block with a hit, also report these `attempts`. |
| `poller_error` | `warn` | The last poller pass did not end in an `exception`. The poller's next good pass clears it, a manual `ingest` does not | The `exception` class name only |
| `unreadable_files` | `warn` | No transcript open `attempt` had an error during the last completed invocation. | Unsuccessful open `attempts` because of permissions, with sources that could succeed subsequently. A vanished file does not count. The SessionStart block, and a recall block with a hit, also report these `attempts`. |
| `aside_files` | `warn` | No historical unreadable stores or kept recovery copies stay | Names of kept store artifacts. Examine the actual reported paths |
| `upgrade_snapshot` | `warn` | No kept pre-upgrade recovery archive exists. | Names of kept `muninn.sqlite.pre-upgrade-*` files. Upgrades without an error keep them. Keep them until the owner checks recovery and retention needs. They can contain transcript text that `erase` cannot scrub. Do not replace newer data with them. |
| `tombstone_key` | Error | `tombstone.key` has its full 32 bytes and exists when keyed tombstones exist. | `missing` or `damaged`. |
| `release_leftovers` | `warn` | No `.pruning-*` release directory is left in `~/.local/lib/muninn` | Their names |
| `launchd_job` | Error | On macOS, launchctl reports one positive PID and a completed PID-only `process` query agrees. Unavailable inspection reports `null`, not success. | Checked PID or a constant observation code above. |
| `roots_readable` | Error | Each provider root that exists is readable | Names of blocked roots |
| `roots_present` | Info | Always. | Names of provider roots that are not on disk. |



`Review` diagnostics use the same four-field check contract:

| Check | Level | Meaning of `detail` |
|---|---|---|
| `review_backlog` | Info | Durable pending and waiting `request` count |
| `review_admission` | Info | `open` when learning is on, `closed` when it is off |
| `review_auth`, `review_config` | `warn` | Attempt-history counts with those finite error codes |
| `review_process_deadline` | `warn` | Recorded deadline errors, or unfinished attempts past the policy deadline with unended calls |
| `review_commit_delay` | `warn` | Overdue `running` attempts without an unended call |
| `review_cancel_delay` | `warn` | Overdue cancelled requests with unfinished attempts |
| `review_abandoned` | `warn` | Recorded abandoned attempts, or unfinished attempts from an older generation |
| `review_coverage_gaps` | `warn` | Eligible records without a success receipt with current matching provenance |
| `review_capture_gaps` | `warn` | New indexed records after the eligible cursor, or eligible records without checked admission provenance |
| `review_erasure_intent` | Info | A `tombstones.jsonl` journal exists; its consistency is unverified |

These checks do not prove provider access, scanning or learning success.
Capture-gap counts do not identify refusal causes.
Do not raise limits or replay history only to clear a count.

## Per-prompt retrieval modes

### Reviewing automatic recall

Each nonempty recall block asks the model to tell you `Memory R<number>: useful/noise/uncertain — reason`.
This is a model `assessment`. It does not guarantee a correct result.
The hook examines prompts and replies.
Its session-start guidance tells agents to use `search --kind prompt` for owner `requests` and `--kind reply` for assistant reports, widening when necessary.

`muninn recall-review [--batch N] [--ack] [--pretty]` reads a batch of at maximum 50 recall blocks.
The default is the earliest unreviewed batch, or the current batch that is not full.
If all batches are acknowledged, it uses the latest batch.
At `delivery` 50 and subsequent nonempty blocks, `systemMessage` gives the human a prompt to paste into Claude or Codex.
It tells the human to run the `review` command and follow `review_prompt`.
The model context keeps a `review` reminder until `muninn recall-review --batch N --ack` acknowledges that next complete batch.

Counts combine Claude and Codex.
Ratings have different counts for each provider.
No timer or background model runs.
The reminder appears when a hook runs.

The private `recall-review.sqlite` has only IDs, timestamps, source locators, excerpt caps, `request`/session fingerprints, and context hashes.
It has no conversation text.
It survives restarts, ordinary log-file replacement, upgrades, and index rebuilds.
Source refs stay authoritative when `event` ids change.

It counts generated context, not proof that the host accepted it or the model read it.
`MUNINN_NO_CALLLOG=1` disables the ordinary call log, not this `review` metadata.
If metadata cannot be written, recall stays available with a review-unavailable instruction.
The call log carries `review_error: 1` and that `delivery` is uncounted.

The report includes `total`, `reviewed_batches`, `batch`, `complete`, `deliveries`, `review_prompt`, `acknowledged`, and (for nonempty batches) `self_assessments`.
Each `delivery` has `id`, `at`, `provider`, `session`, `prompt_sha256`, `scope_ids`, `context_sha256`, and `characters`.
Its last `sources` has the ref and excerpt cap.
Its last `knowledge` has `id`, `revision`, `evidence_refs`, `cap`, `sha256` of the source preview, and `evidence_sources`.

After indexing, a unique `request` supplies `request_ref` and `request_preview`.
Its redacted text hash, provider/session, and timestamp must agree, with a difference of at maximum two minutes.
If not `request_status` is `unresolved`.

`assessment` contains the model's matching `Memory R<number>` rating before the next prompt, its source ref and `reason` preview, or `unassessed`.
Previews need opening `originals` before factual claims.
The `request` and `assessment` previews are redacted again before printing, with text indexed before a redaction rule existed.
A database that becomes unavailable during `request` or `assessment` resolution reports a JSON error with exit code 4.

Historical full block text is not kept, and current knowledge can be different.
Use `muninn know show K_ID --revision N` with the recorded UUID and revision to examine that knowledge version.
Its E_UUIDs are `evidence` identities, not arguments to `muninn open`.
Each recorded `evidence_sources` row maps an `evidence_id` to an openable `ref` and the `original` `line_sha256`, bound to that same knowledge revision.

Open that recorded `ref` with `muninn open REF --context 3`.
Check for `hash_ok: true` and `provenance.line_sha256` equal to the recorded hash.
A recorded locator does not prove the source stays available or unchanged.
Missing mapping fields or null `ref`/`line_sha256`, missing `originals` and changed hashes stay unresolved.

Do not use current knowledge or other `evidence` as a replacement.
The preview `sha256` identifies the pre-rendering source preview, not the complete knowledge body or redacted, shortened historical block.
The mapping records no transcript text or filesystem path.
`--ack` needs `--batch`, a completed batch, and sequential `review`.

Invalid arguments exit 2.
Unavailable metadata or transcript database exits 4.
Acknowledgement records `review`, not approval of recall quality.
Follow `review_prompt` and report missing `evidence` before acknowledging.

The ordinary call log at this time distinguishes selected `returned_ids`/`knowledge_ids` from `delivered_ids`/`delivered_knowledge_ids` that survive shortening.
Knowledge IDs use `K_` UUIDs.
`event` IDs stay numeric.
A recorded block also has its numeric `delivery_id` and, when due, `review_due_batch`.

No query text or fingerprints enter the prompt-hook call log.
`request` fingerprints live only in the `review` database.

### Switch behavior

**Function.** Before each prompt, `muninn hook prompt` can add current eligible knowledge previews.
It follows these with at maximum three prior prompt/reply `events` from the mapped project.
It does not include the caller's own session.

At minimum three different query terms are necessary. The hook skips slash commands.
UTF-8 caps are 1,500 bytes for Claude and 900 for Codex.
When `recall.off` exists, the prompt hook prints `{}`.

When `recall.advisory` exists and `recall.off` does not, ordinary prompts receive the static `<muninn-retrieval>` instruction.
This opens no database and delivers no automatic recall.
Empty prompts and slash commands print `{}`.
With no mode file, automatic recall is on.
A nonempty automatic recall block tells the agent to examine `K_` records with `muninn know show`.
Find the `E_` ID in `evidence_sources`, then open that same `ref`.

The IDs themselves are not `muninn open` refs.
Check the opened original's hash against the source's `line_sha256`.
`MUNINN_HOOK_DISABLE=1` silences all hooks.

The static instruction tells the model to use `search` on demand when project history is necessary.
Start with `--kind prompt` for owner decisions or `--kind reply` for assistant reports.
Refine or widen as necessary.
Open `original` refs before using earlier claims.

`K_` IDs go through `muninn know show`.
`E_` IDs are not open refs.
Missing related `evidence` must be reported as unknown.

This instruction does not run `search` or count as an automatic recall `delivery`.

Checked SessionStart refreshes use `startup`, `resume`, `clear` and `compact` `events` in Claude and Codex.
They make accepted notes and a compact guide index as untrusted data.
Claude receives up to 4,000 bytes.
Codex receives up to 2,800 bytes.

The block keeps the frame, the usage line and the search and open lines.
The memories are one numbered list between two bars of `▓`:
a header `▓ MUNINN MEMORY · <scope> · <shown> of <total>`, each memory's text wrapped at 70 characters with a `When:` line for conditions, then one code line `▓ <n>: <K_ID>` for each memory.
A guide shows its title.
The bar is as wide as the longest line, from 20 to 72 characters.
The block has no kind, revision, scope JSON, evidence IDs or expiry.

This read-only startup context does not make a learning policy necessary.
The order is decisions and preferences, then constraints and procedures, then facts and other notes, then guide titles; newest first inside each group. Global memories use the same order.
Memories are taken in order until the next one does not fit; that one starts page 2.
A memory larger than a whole page shows `memory too long: run muninn know show --proof K_ID`.
When memories are left out, the footer `▓ <k> more memories. Before you write code, run: muninn know context --page 2` points to the next page (Codex adds `--provider codex`).
The `usage` text tells agents to check current revisions and open cited `originals`.

Preview eligibility is not a truth guarantee.
Claude/Codex `systemMessage` lists the displayed items with control characters replaced.
It distinguishes knowledge that does not fit from no eligible knowledge.
The status's content is not logged.
Prompt recall adds a human-facing line only when a 50-`delivery` `review` is due.

End/PreCompact capture admission is different from startup refresh.
An installed learning policy is necessary.
Muninn acknowledges only a durable `request`.
If SQLite is busy, the hook reports an empty response and logs `review_enqueue_busy` without content.

Subsequent recovery can capture subsequently indexed content, not prove the missed `event` or cutoff.
Cursor waits for the next native import or explicit full index.
A recovery `gap` is not proof of coverage.
Cursor startup uses its native `sessionStart` envelope (`additional_context`).

Context on resume or after compaction stays unproven.
Explicitly run `muninn know context` after continue or compaction.
Context injected cannot be retracted.

**Default for new installs: OFF.** `--fresh` makes `recall.off` (mode 0600) and prints how to turn recall on.
`--upgrade` does not change the mode files, so a current machine keeps its selected mode.
If the two files exist, off wins.

**Rationale.** Automatic recall can introduce irrelevant material from a different project.
Retrieval relevance has not been revalidated, so new installs disable recall until that validation passes.
See `docs/adr/0007-recall-off-by-default.md`.

| To | Run |
|---|---|
| On: automatic previews | Remove `recall.off` and `recall.advisory`. |
| Advisory: model decides when a query is necessary | Make `recall.advisory` with mode 0600. Remove `recall.off`. |
| Off: no prompt context | Make `recall.off` with mode 0600. Remove `recall.advisory` |
| Check | `ls ~/.local/share/muninn/recall.{off,advisory}`. Off wins if the two files exist. |

Mode files must be 0600, if not `muninn doctor` fails `file_modes`.
Their contents are ignored.
The change takes effect on the next prompt.
A new service start is not necessary.

These mode files control prompt recall only.
They do not block SessionEnd or PreCompact capture.
They do not block an approved learning runner.
Experimental learning needs its policy, host, `capability` and admission checks.

If no mode file exists, automatic prompt previews stay on.
Learning can continue when its checks pass.

## Files in the data directory

- `muninn.sqlite` is the database. `writer.lock` is the writer lock.
- `status.json` is the poller heartbeat, with counts only.
  `last_error` and `last_error_at` give the class and time of the last pass with an error.
  The next good poller pass clears them. Manual `ingest` does not.
- `calls.jsonl` and `calls.jsonl.1` have one allowlisted line for each CLI call: IDs and counts, no text. The log rolls at 1 MiB.
- `poller.log` and `poller.log.1` have poller events under the same limit.
  Launchd restarts the poller after each exit; only `launchctl bootout` keeps it down.
- `tombstones.jsonl` has erase records.
- `tombstone.key` has 32 random bytes, mode 0600. Muninn makes it the first time an erase needs it, and it survives `rebuild`.
  If it is missing or damaged while keyed tombstones exist, `ingest`, `rebuild` and `erase` exit 4 with `tombstone_key`, and `doctor` fails `tombstone_key`.
- `learning.json` holds the four learning settings (see Learning settings).
- `muninn.sqlite.unreadable-*` and `muninn.sqlite.recovery-*` are kept unreadable and rebuild copies.
- `muninn.sqlite.pre-upgrade-<sha>` is the private copy made before an upgrade that changes the store format (mode 0600).
  The copies can hold erased text that `erase` cannot scrub; `erase` lists them in `aside_files`, and `doctor` warns while they exist.
  Muninn removes none of them. Remove one yourself after you have checked that you no longer need it.
- `muninn.sqlite-journal` is SQLite's rollback journal. A remaining journal fails `unowned_journal`.
- `recall-review.sqlite` and its journal keep private delivery metadata and review checkpoints, with no transcript text.
- `recall.off` and `recall.advisory` select the prompt retrieval mode. Each must have mode 0600.

Any other file fails `unexpected_files`.
