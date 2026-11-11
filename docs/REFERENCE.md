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
| `detail` | With `store_unavailable` only: a refusal message written by Muninn, at maximum 80 characters, such as `cutover_required`. It does not use database text. The same string appears in that call's `calls.jsonl` line and hook diagnostic line (`hook session-start` and `hook prompt` lines). |

**Exit codes:** 0 ok.
1 `doctor` found an error-level problem.
2 refused or bad `usage`.
3 busy (a different writer holds the lock, retry).

4 means an unavailable database, a crashed writer's hot journal, or missing or damaged `tombstone.key` (`error` `tombstone_key`).
Recovery refusals include a stable `reason`, a plain-language `why`, and a `next_action`.
`muninn --version` prints `muninn <version>`.

**Environment:** `MUNINN_HOME` data dir (default `~/.local/share/muninn` on macOS/Linux or `%LOCALAPPDATA%/Muninn/data` on Windows).
`MUNINN_CURSOR_DB` optional native absolute path to Cursor's `state.vscdb`.
`CODEX_HOME` and `CLAUDE_CONFIG_DIR` select provider homes (default `.codex`/`.claude` below `USERPROFILE` on Windows, `HOME` elsewhere).
`MUNINN_ROOTS` JSON map of provider root name to path (tests).

`MUNINN_PYTHON` interpreter for `bin/muninn`.
`MUNINN_PRETTY=1` indent.
`MUNINN_NO_CALLLOG=1` no `calls.jsonl` line.
`MUNINN_HOOK_DISABLE=1` hooks print `{}`.

`CLAUDE_CODE_SESSION_ID`, `CODEX_SESSION_ID`, `CODEX_THREAD_ID` identify the calling session, which search leaves out unless told not to.

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
`stages` in a `search` or `hook prompt` line of `calls.jsonl` can include integer flags 0/1.
A flag appears only with value 1.
`miss_zero` means searchable terms but no result.

`miss_outside` means results exist but none are in scope.
For `search`, `miss_noterms` means no searchable terms.
`stages` then has only this key.
For the hook, `miss_floor` means the term floor dropped all read `events`.
This excludes pages not read when the pool reached its page limit.

The hook's `stages` has `floor_dropped`, `returned`, and `pool_error`.
`pool_error` is set when one or more result pages have an error.
In that condition, `miss_floor` is not included.
If the query gave an answer, it also has `matches`, `in_scope`, and at maximum one miss key.

The log has `n_terms`, but no query hash or text.
`miss_outside` depends on the working directory.
A query external to the repo scope counts as external, although `--all-projects` could find results.

A flag is set only if the call reports no hit and no knowledge.
A call with an error sets no flag.
A skipped prompt logs no `stages` and no flag.
The hook skips prompts with fewer than 3 query terms or a first character `/`.

`MUNINN_NO_CALLLOG=1` stops the log.
These flags describe one call and do not prove that no related answer exists.
Check actual misses with labeled questions.

## Current knowledge and learning

Knowledge IDs are `K_` plus 32 lowercase hexadecimal characters.
`request` IDs use `R_` with the same shape.
Muninn rejects numeric knowledge aliases.
List/search report `knowledge: [KnowledgeView, ...]`.

A single typed view keeps its fields at this JSON object's root, with the same notice, freshness, and logging metadata.

| Command | Meaning/output |
|---|---|
| `muninn know list [--all-projects] [--task TASK]` | Current eligible `KnowledgeView` objects, newest first. |
| `muninn know search QUERY [--all-projects] [--task TASK]` | SQLite FTS5 search of current permitted knowledge. No embeddings. |
| `muninn know show K_ID [--revision N]` | `KnowledgeView` fields plus `evidence_sources`. This maps each `E_` ID to the transcript `ref` and `line_sha256` for that revision. Unavailable sources have null locators. Historical revisions need `--revision`. Explicit show can keep withdrawn content. |
| `muninn know context [--task TASK]` | `KnowledgeContext`: `schema_version`, complete note views in `notes`, compact `guides` (`id`, `revision`, `title`, `scope`), `unresolved_ids`, `truncated`. Bound by installed policy's `context_bytes`. |
| `muninn know propose --input FILE` | Strict `ProposalInput`, then a checked current local `attempt` and `Receipt`. No model invocation. |
| `muninn know retract K_ID --expected-revision N --reason TEXT` | Checked withdrawal of that current revision. Reports `KnowledgeView`. |
| `muninn learn request --session REF [--from-line N --through-line N] [--policy PATH] [--task TASK]` | `RequestView` for a bounded `request`. Paired `bounds` or activation/uncompleted boundary through the latest complete record. `--task` binds the `request` to one task in the source project. |
| `muninn learn setup` | Preview history or save an explicit record scope. |
| `muninn learn list [--state STATE]` | `requests: [RequestView, ...]`. `state` is `pending`, `running`, `waiting`, `completed`, `failed` or `cancelled`. |
| `muninn learn show R_ID` | `ReviewDetail`: `request`, `attempts`, durable calls, governed `evidence`, receipt/null, `correction_status`, and safe `diagnostics`. Content is shown only when specified, not logged. |
| `muninn learn explain R_ID [--human]` | Gives `request_id`, `correction_status`, and `explanation` as JSON. The explanation covers kept history, missing information, recorded changes, current admission, recovery, and dismissal. `--human` also prints it to stderr. Stdout stays JSON. Muninn displays no transcript or proposal text. |
| `muninn learn dismiss R_ID` | `request_id` and `dismissed` (boolean). Remove only that request's recorded error notice. A duplicate dismissal reports `false`. An unknown `request` reports `request_unavailable`. |
| `muninn learn cancel R_ID` | Current `RequestView` after cancellation. |
| `muninn learn retry R_ID` | Explicit restart of the same `request`, with cancelled `requests` when current guards permit. Completed `requests` keep their receipt/`state` without a different commit. |
| `muninn learn resume --policy PATH --failed-request R_ID` | Turns stopped ongoing Codex admission on after you check the latest specified `rejected_authority` error. The file must equal the installed policy. All `attempts` must be terminal and all calls completed. Other error types, abandoned `attempts`, and pending work are not permitted. `failed` `requests` stay `failed`. Output is `{"admission":true,"policy_id":...}`. |
| `muninn learn replay --session REF --from-line N --through-line N --policy PATH` | Explicit bounded historical `request` through the same `evidence`/commit path. No automatic archive `review`. |


Command syntax:

```text
muninn learn setup --mode now|all [--project PATH] [--confirm SCAN_DIGEST]
```


`REF` for learning is an indexed `provider:thread_id` or an unambiguous session root.
`Request` creation is admission, not completed `review`.
Missing or invalid installed frozen policy refuses with `review_policy_required`.
An explicit policy must be the same as the admitted installed policy.
It must not supply guessed model settings.

Policy records explicit runner/model/`reasoning`, bytes/`rounds`, deadlines, retries/backoffs, concurrency, and budget.
This CLI does not install a policy.

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

The current database needs schema version 2 and the `store_meta` marker `muninn-knowledge`.
Ordinary writers and rebuild refuse unmarked databases or a different current format.
A marked format-1 database needs a checked installer upgrade before current-format operations.
The upgrade keeps a raw private recovery archive and copies governed data into the current format.

A completed format-1 owner with a changed macOS device number can use the installer's explicit legacy disk confirmation.
This path checks that historical erasure is complete without writing the format-1 database.
It keeps supervisor output in the legacy format before runtime selection.
Pending historical erasure or an uncertain owner needs the checked format-1 recovery procedure before migration.

During `--upgrade`, the installer copies a schema v3 database with the accepted table contract into the marked current format.
Recognition uses its schema version and table set, not the previous release commit or SQL formatting.
The full `original` database stays in a private pre-upgrade archive, with the legacy knowledge ledger.
That legacy ledger is not imported into the current format.

Other unmarked layouts stay unchanged and refuse upgrade.
A numeric pragma alone is not a format or maintenance-authority certificate.
The specified inventoried legacy release uses its own exclusion and reboot procedure below.

A current-format installer upgrade keeps the live pathname.
After Muninn proves the pause, it fences `review` `attempts`, keeps unknown charges and validates the permitted policy before activation.
One actual maintenance owner spans pin/config.
The writer is released during those steps and the owner before service start.

After possible live mutation, an error keeps current data and the selected runtime/configuration.
It keeps the service stopped and records `outcome: recovery_required`.
It does not put back an archive above newer data.
Current governed archives are kept.
Each installer path that replaces a live database needs its durable conservative publication record before replacement and runtime/configuration rollback decisions.

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
`replacement_id` is a different new D ID for edit, if not null.
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

## Exact legacy exclusion and forward cutover

For the specified inventoried legacy release `5c1a69b0edff81df80836d72a80971a91a8f9520` on macOS/Linux, the installer can `stage` managed launch exclusion.
This makes it necessary to stop previous manual commands, source/module invocations, imported clients, and custom tasks during upgrade and recovery.
Do not start them during that interval.
It keeps the previous database, tombstone key and erasure journal.

It prepares the candidate runtime.
It replaces the stable launcher with a terminal maintenance response.
It keeps the previous release in the private rollback directory.
It removes the permanent service registration.
The previous `current` link deliberately stays selected but dangling.

One stable private installer lock serializes upgrades and direct recovery.

Only after those exclusions are durable and revalidated does the installer record `outcome: restart_required` and an accepted boot identity.
Ordinary commands stay unavailable.
Hooks report compact `{"error":"maintenance","reason":"restart_required"}` with exit 0, without queue acknowledgement.
The installer exits nonzero because the upgrade is not complete.

The same `--upgrade --repo ... --sha ...` discovers the pending record before ordinary preflight.
Same-boot continue refuses.
A subsequent boot must have the same source `original`, selected interpreter, candidate, private record, and all exclusions.

Missing or changed `evidence` yields refusal or `recovery_required`.
Replacing a fence that no longer exists cannot use its previous boot baseline again.

The public installer continues after checked subsequent-boot closure.
The installer keeps the operation lock and one maintenance owner.
It validates the specified schema3 source and strict erasure intent.
It keeps a durable private `muninn.sqlite.pre-upgrade-*` archive.

It copies the seven base tables into an unpublished marked2 target.
Indexed Cursor and missing-original history survive.
Legacy knowledge stays in the archive.

Target replay completes pending erasure before replacement, without changing the legacy source, external key or journal.
Archives and copies that did not finish stay in the erasure `residue` report.

Before one replacement `attempt`, the private record persists `publication: possible` with source, candidate, archive, home, lock, intent and prepared runtime bindings.
A replacement error can mean the new store is live.
Recovery reads actual identity and format, keeps each archive, and does not put back previous data/runtime or promote an orphan.
An unchanged `original` can be retried by building a new candidate.

A proven marked replacement continues with the current witness rules.
Missing/uncertain witness after compatible runtime exposure cannot be repaired with a previous installer boot observation.

Compatible configuration completes while the maintenance launcher stays installed.
The last writer revalidates the live path and replays new erasure intent.
First policy activation, if specified, completes before removing that launcher.
All maintenance roots close before service start.

Provider configuration that stopped without a record stays excluded with `recovery_required`.
Replaying a plugin mutation could overwrite its `original` before-values.
Legacy operations that complete without an error keep their private recovery record and archive metadata.
The preparation-only `verify_legacy_resume` interface stops at `legacy_cutover_pending`.
It does not do publication.

### Default learning policy

Each clone includes the default policy template at `tools/default_codex_policy.json`.
During an install that uses the current default home, Muninn applies the policy template when the database has no learning policy.
It does this only when the host can run the Codex runner.

The runner needs Apple silicon macOS.
Keep a signed Codex CLI on `PATH`.
Keep the canonical Codex sign-in directory available before installation.
Muninn binds the policy to the host, data directory and executable.

If a requirement is unavailable, learning stays pending.
Trust Codex hooks as one more provider step.

The default uses Codex CLI, model `gpt-6.1-sol`, and low `reasoning`.
Revision 3 allows four workers and three total `attempts` for each `request`.
Each `attempt` allows two calls, 1 MiB of input, 32 KiB of output, 12,000 context bytes, and 120 seconds.
The first retry waits 5 seconds.

The second retry waits 30 seconds.
Manual and live `requests` go before queued history.
In each group, Muninn works on earlier `requests` first.
SQLite writes stay on the `main thread`.

Before writing, Muninn compares current knowledge with the worker's `original` knowledge snapshot.
A changed snapshot needs a new `attempt` in the same `attempt` limit.

Revision 3 continues after a rejected result or an exhausted temporary error.
A temporary error is a deadline or an unavailable runner.
A stale knowledge revision can also use a remaining `attempt`.
Invalid output and unsuccessful authority, scope, or `evidence` checks receive no automatic retry.

Current policy revisions keep their earlier controls.

Missing setup keeps work queued.
The background runner can check again while it runs and learning stays on.
Setup checks wait at minimum 60 seconds.
This is not a promised check time.

Muninn does not reopen an owner pause.
The `process` monitor reports an outcome only after it contains and collects the child `process`.
An uncertain `process` keeps learning blocked.
A reported Codex tool step also stops admission.

The runner keeps its documented limits on tool `isolation`.

Before first activation, Muninn freezes the policy and local paths in its private install record.
It commits the policy and source boundaries together.
Recovery uses the frozen policy.
It does not select a different policy during recovery.

An upgrade can refresh an eligible repository default from revision 1 or 2 to revision 3.
The new `approved_at` value must be after the previous value.
No `attempt` can stay unfinished.
The change keeps the host, runner, prompt, budget identity, activation time, and admission `state`.

Previous `request` policies stay immutable.
Unstarted old-policy `requests` become cancelled with `cancel_code=policy_replaced`.
The chosen learning scope controls new `requests`.
Custom policies keep their current limits.

An owner pause stays in force unless the person explicitly accepts the installer restart prompt.
`--enable-learning` supplies this more approval for an unattended install or upgrade.
Each recorded call must be completed, and each `attempt` must have ended.
`running` `requests` prevent this restart.

The installer can recognize an unchanged legacy default without a `policy_source` marker.
It keeps the recorded runner version and host settings.
Changed custom policies need a newly approved `--review-policy PATH`.
The flag alone cannot approve them.

To customize the policy template, edit and commit `tools/default_codex_policy.json` in your private clone.
For revision 3, edit `revisions.3.concurrency`, `revisions.3.max_attempts`, and `revisions.3.retry_backoff_seconds`.
Use at maximum four workers and three total `attempts`.
Give one backoff value for each retry.

The `limits` object controls bytes, context, `rounds`, and deadlines.
These values can be decreased in the enforced `bounds`.
Keep the runner, model, `reasoning`, host binding, and project-scope settings.
Record approval with `approved_by` and UTC `approved_at`.

### Learning scope and history

The installer asks for a record scope when no selection exists.
It keeps the selection during subsequent upgrades.
An ordinary upgrade with no new learning or policy `request` keeps the recorded scope and its `original` boundary.
It does not tell you to `review` more history or queue that history to complete the upgrade.

An explicit scope or policy `request` uses the usual `review` and consent checks.
A kept From at this time selection keeps its `original` start time.
After policy replacement, the installer confirms eligible replacement `requests` before queuing them.
It does not silently discard a previous queue.

`now` excludes indexed history at the install boundary.
Earlier or undated records are not included in the scope when their source is indexed subsequently.
`all` includes eligible older records and future records.
Global records stay excluded.

All available history also accepts checked earlier transcripts indexed after setup.
Native-source checks apply.

Before history starts, Muninn scans physical sources and shows the work for confirmation.
It groups same-project records into intervals of at maximum 200 lines.
Source, scope, erasure, and read limits apply.
The scan names excluded source ranges and their causes.

It uses recorded `request` identities to prevent duplicates after interruption.
Completed ranges and owner cancellations stay excluded.

Use these optional commands to change the selection:

```sh
muninn learn setup --mode now
muninn learn setup --mode all
muninn learn setup --mode all --confirm SCAN_DIGEST
```

The history preview reports `confirmation`, `eligible_requests`, `excluded`, `excluded_spans`, `estimate`, and `scope`.
Copy its `confirmation` value into `SCAN_DIGEST`.
Confirmation includes all traversed `original` bytes and indexed records.
If history changes before admission, confirmation fails with `history_scan_changed`.

Reindexing changed `originals` does not keep an earlier confirmation.
Preview the scan again before confirming.
`--project PATH` limits automatic learning to one indexed project.
From-now mode applies to all projects and refuses `--project`.

An active `review` prevents scope changes with `learning_busy`.

Time estimates use comparable completed calls.
The estimate shows its sample count and confidence.
It uses full worker batches, with a full batch for one `request`.
`seconds_low` and `seconds_high` describe first-attempt work.

`retry_seconds_high` includes permitted `attempts` and retry backoffs.
`api_cost_decimal` describes first-attempt cost.
`retry_api_cost_decimal` includes permitted `attempts`.
These estimates exclude setup and database waits.

API cost estimates need reported costs from comparable calls.
Unreported cost stays null.
Subscription cost and remaining subscription allowance stay unknown.
Capture-gap counts are not multiplied into a call or cost estimate.

### Learning failure messages

At session start, `systemMessage` shows a learning step when owner input is necessary.
This message also shows with `recall.off` or `recall.advisory`.
`Request` notices include their `request` ID in commands to examine and dismiss them.
Muninn shows setup notices before eligible `request` notices.

Muninn then selects the newest eligible `request` notice.
A newer contained error cannot hide an earlier necessary step when admission cannot safely continue.
If admission cannot safely continue and no necessary step stays, Muninn shows a contained issue with current pause guidance.

With open admission and a correct stored policy and digest, contained `request` errors stay quiet at startup.
These include rejected proposals, exhausted corrections, bounded retries, and deterministic source-read limits.
The finite contained codes are `deadline`, `unavailable`, `rejected_revision`, `rejected_authority`, `rejected_validation`, `rejected_evidence`, `rejected_erased`, and `rejected_scope`.
They also include all five `evidence_*limit` codes below.
Sign-in, `capability`, binding, `security`, native tool `events`, uncertain `processes`, approval errors, and unknown codes stay in the display.

Setup notices stay readable.
`prompt_limit` stays readable because the maintainer must examine the prompt budget.
The complete prompt must fit the current input cap.
This includes transient untrusted correction baselines.
Muninn fails before a different reservation or launch, without truncation, a limit increase, or an automatic retry.

Do not retry the `request` unchanged.

Closed admission stays readable without a recorded issue.
A missing, invalid, or mismatched recorded policy also gives a startup step.
Muninn reads recorded admission and validates the stored policy and digest for this decision.
Recorded step wording does not prove current admission.

These checks do not prove live host, account, or worker health.
Run `muninn doctor --pretty` for current checks.
Startup changes no recorded issues, `requests`, admission, or history.
It displays constant software text.
It does not display question bodies, source text, or model output.

You cannot dismiss a setup notice with a `request` ID.
`muninn learn show R_ID` adds `diagnostics` with `stage`, cause, step, and code.
To read an explanation in ordinary language, run:

```sh
muninn learn explain R_ID --human >/dev/null
```

`--human` prints the last redacted explanation to stderr.
The command writes the JSON answer to stdout.
Without `--human`, read the `explanation` string in the JSON answer.
`--pretty` changes only JSON indentation.

The explanation uses kept metadata: records that describe the `request`.
It displays no source text.
Muninn does not keep model proposals with errors.
Their proposed sentence and unchanged source excerpt are unavailable.

`Evidence` counts describe the kept input to the `request`.
They do not reconstruct a rejected proposal.

Use `learn show` to examine `evidence` with the current access checks.
Changed or erased `evidence` can have no usable excerpt.
Only the commit receipt proves recorded changes.
A rejected batch commits no part of its knowledge changes.

Missing launch records do not prove that no model call occurred.
Recorded error phases identify only the recorded phase.

`correction_status` describes the latest `attempt` with a durable correction marker, in start-time and `attempt`-ID sequence.
`requested` means a marker exists, but its specified next `round` has no recorded launch.
`attempted` means that next `round` launched in the same `attempt`, but its correction result is unproved.
`succeeded` needs a completed next `round` and a receipt from that same `attempt`.

`exhausted` needs a completed next `round` and a terminal error from that same `attempt`.
Without a correct marker or sufficient kept `evidence`, the status is `unknown`.
Two calls alone do not prove correction: they can be a read and a last result.
A subsequent ordinary success cannot prove that an earlier correction succeeded.

Clearing an error notice keeps correction markers.
The receipt independently proves recorded changes or `no_change`.

New result errors record one measured phase: `native output`, `model format`, `candidate validation`, `controlled read`, `original evidence`, or `commit`.
Setup and launch checks keep their current phase labels.
Missing historical phases stay `not recorded`.
Muninn does not infer them from codes or call counts.

Muninn does not invent finer information.
The explanation reports current admission and setup notices independently from `request` history.
Admission controls if learning can accept work.
A correct stored policy does not prove that a worker runs or setup checks pass.

`muninn learn dismiss R_ID` removes the recorded notice for that `request` only.
It keeps the `request`, `attempts`, calls, `evidence`, receipt, and `attempt` error codes.
`failed` `requests` stay in `learn list --state failed` and `learn show`.
Dismissal does not retry a `request`, enable learning, or resolve a setup error.

It removes the recorded notice's `stage` and step text.
Show then builds diagnostics from recorded `attempt` error codes.

The `stage` is `not recorded`.
The cause and step use their default text.
A new error after dismissal can make a different notice for the same `request`.
Upgrades keep recorded notices.

Startup filters only the displayed step.
Contained errors stay available through `learn list`, `learn show`, and `learn explain`.
After you dismiss one `request` notice, an earlier eligible notice can show.
Work that completes without an error clears the notice for that `request`.

`doctor` adds `review_attention` for a stored issue, with a contained error hidden at startup.
These fields contain constant software text and identifiers.
They contain no transcript or model output.

`Evidence` selection and window-boundary verification use one physical source scan.
Muninn then rereads the selected record before admission.
The two reads share the current operation budget.
The limit includes source traversal, not only selected text.

Muninn keeps no source text between operations.

| Code | Meaning and response |
|---|---|
| `deadline` | The `review` exceeded its deadline. Muninn uses a remaining `attempt`. |
| `unavailable` | The runner did not finish successfully. Muninn uses a remaining `attempt`. |
| `config` | Muninn could not check setup and did not record the specified cause. The recorded `stage` cannot identify the cause more accurately. Run `muninn doctor --pretty` for current checks. Give unresolved safe diagnostics to the maintainer. |
| `prompt_limit` | The complete `review` prompt exceeded its input byte limit. Tell the maintainer to examine its prompt budget. Do not retry unchanged. Configured limits stay unchanged. |
| `evidence_limit` | Original-source verification exceeded its byte, record, or database-work limit. Do not retry the `request` unchanged. Tell the maintainer to examine its source-read budget. A narrower window can make a read of earlier records necessary. Muninn fails the `request` without an automatic retry, with limits and learning admission unchanged. Earlier `config` records cannot repair a cause that Muninn did not keep. |
| `evidence_byte_limit`, `evidence_record_limit`, `evidence_work_limit`, `evidence_selection_limit` | Diagnostics identify physical bytes, physical records, database work, and accumulated selected content, in that sequence. These allowances limit source verification. They are different from memory storage capacity and model context limits. Tell the maintainer to examine the allowance. Do not retry unchanged. Without a historical record, Muninn cannot reconstruct configured limits or measured counters. |
| `capability_changed` | The approved runner setup changed. Muninn waits for that approved setup to be available again. |
| `capability_unavailable` | A necessary runner executable, proof, or schema file cannot be read. Muninn waits for the approved files. |
| `capability_invalid` | A runner `capability` or proof file has an invalid format. Muninn waits for the approved file. |
| `capability_path` | A runner path is relative or resolves to a different path. Put back the approved path. |
| `capability_storage`, `capability_executable_storage` | Ownership or access checks did not satisfy the rules. Put the approved file protections back. |
| `capability_executable_provenance` | The source of the executable cannot be checked. Put the approved executable back. |
| `capability_platform`, `launch_input_platform` | The approved runner cannot use this operating system or processor. Use the approved host. |
| `capability_limit` | Runner setup exceeded a file byte limit or scan limit. Put back the approved setup. |
| `codex_experimental_boundary` | An older setup refusal did not record the specified mismatch. Its cause stays unknown. |
| `codex_approval_required` | There is no recorded Codex approval. |
| `codex_data_home_changed` | The recorded and current data directories differ. |
| `codex_capability_status` | The runner status differs from the approved setup. |
| `codex_platform_changed` | The operating system or processor differs from the approved setup. |
| `codex_host_changed` | The recorded computer ID differs from this computer. Why it changed stays unknown. |
| `codex_project_required` | This `review` has no project binding. |
| `native_prompt_instruction` | `Review` instructions differ from the necessary Codex format. Put the approved instructions back. |
| `launch_input_storage` | A `review` setup file is external to its necessary private directory. Put back the approved location. |
| `runner_unsupported` | The selected runner has no accepted launch method. Use the approved runner. |
| `rejected_authority` | The proposed change has no checked owner `evidence`. Muninn rejects it and continues. |
| `rejected_revision` | Recorded knowledge changed during `review`. Muninn rebuilds the `review` in the `attempt` limit. |
| `rejected_validation` | The result differs from the necessary format. Muninn rejects it and continues. |
| `rejected_evidence` | Necessary source `evidence` is unavailable, changed, or cannot be checked. Muninn excludes the `request`. |
| `rejected_scope` | The proposed change is external to the admitted project scope. Muninn rejects it and continues. |
| `rejected_erased` | An erase record excludes the `evidence`. Muninn excludes the `request`. |
| `process_identity_unverified` | Muninn cannot check that an earlier `process` stopped. Learning stays blocked. |
| `native_tool_event` | Codex reported a tool step. Examine that step before enabling learning again. |

Muninn can retry safe calls and recheck approved local setup.
Setup errors keep queued `requests` available.
The background runner can check setup again while it runs and learning is on.
Its setup wait is at minimum 60 seconds.
No check time is guaranteed.

A recorded notice starts no timer when you read it.
An unsuccessful check keeps or replaces the notice.
A correct related setup check or launch clears it.
Clearing a notice does not prove recorded knowledge.

Muninn makes current notices from finite safe codes.
It does not use recorded human text again.
It cannot sign in for a person, approve changed `security` bindings, put back missing `original` transcripts, or infer provider allowance.
These steps need account access, approval, `original` data, or provider information that Muninn does not have.

You can also give a custom `--review-policy PATH` for first activation of an unconfigured database.
The file must be bounded, strict UTF-8 JSON.
Muninn freezes the unchanged input.
A different policy cannot replace a configured policy.

Do not edit the database by hand.

Before each launch, Muninn resolves the selected Codex path and checks its macOS signature.
It connects the executable to a private `capability` and uses constant launch arguments.
This `capability` does not fully isolate Codex tools or steps.
The default policy template records this limit.

The finite pilot account is one more control.
It does not prove runner status or full C6 `acceptance`.
See [Codex learning qualification](CODEX_LEARNING_QUALIFICATION.md) for the native probe, test `evidence` boundaries and remaining `assessment` requirements.

The procedural tests cover five synthetic episodes.
They are success with a condition, different wording of that condition, no condition, error with an unknown cause, and duplicate recalled advice.
They test actual capture, `evidence`, commit, and retrieval with a scripted child and isolated databases.
Checks examine applicability, pitfalls, `original` `support` IDs, and actual displayed or withheld content.
The target labels stay external to runner prompts.

Other frozen retrieval checks show that opening a guide adds no `support` edges.
These tests prove contract coverage, not measured model benefit or semantic judgments.
Project procedures can cite reported or untrusted `evidence`.
The tests do not prove causal diagnosis.
They do not prove universal prevention of duplicate advice becoming new `support`.

Windows legacy boot and termination proof, and native live `qualification`, are more requirements.
During recovery, keep the private operation record, source and archive, key, and journal.
Manual restoration of the previous launcher or service is unaccepted.

## Running and maintaining

| Command | What it does |
|---|---|
| `muninn ingest [--full]` | Catch up with Claude and Codex provider transcripts at this time. `--full` rescans everything and imports Cursor history from `MUNINN_CURSOR_DB` when supplied, or `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` on macOS. Windows and Linux need an explicit database path. No layout is guessed. Cursor data is read-only and is not polled. Answer `ingest`: `files_seen`, `files_changed`, `events_added`, `events_removed`, `skipped_files`, `unreadable_files`, `skipped_lines`, `failed`, `errors`, `missing`, `duration_s` |
| `muninn serve [--interval S]` | The managed poller loop (default 60 s). Prints nothing |
| `muninn stats [--usage]` | Counts, see below |
| `muninn doctor` | Health checks, described below. Exit 1 if one or more error-level checks are `false`. |
| `muninn recover [--confirm-legacy-volume]` | Continues a maintenance pause on macOS or Linux. It starts no new erase `request`, but can apply intent from `tombstones.jsonl`. See the recovery command information below this table. |
| `muninn compact` | VACUUM the database with the writer lock held. Answer `compact.bytes_before`/`bytes_after`, `db_space`. Refuses without about one database of free disk |
| `muninn rebuild` | Builds a checked marked current database again. Copy each governed family and keep DB-only Cursor and missing-source history. Re-ingest all transcripts and replay current erasure intent. Fence admission and `attempts`, and keep `muninn.sqlite.recovery-<id>` before candidate work. Unaccepted, unreadable, changed, or unfinished sources refuse, and publication does not put an archive back automatically. See the recovery contract below. |
| `muninn erase` | Forgets selected `events` and related revisioned knowledge, `evidence`, `requests`, and unresolved material. Without `--yes`, it is a dry run. Live erasure coordinates a whole-service pause before durable tombstones and content scrubbing. The answer includes cleanup limits and nested `erasure_status`. See the erasure section below. |
| `muninn hook` | Session and prompt hooks: payload on stdin, JSON on stdout (see the README) |
| `muninn hook` | Capture bounded learning windows. Cursor imports the specified conversation before capture, with preCompact bounded by `message_count`. SessionEnd does not use that field. All hooks report compact provider-compatible JSON and exit 0. |

Command syntax:

```text
muninn erase --session S | --event REF | --match TEXT [--dry-run] [--yes]
muninn hook session-start|prompt --provider claude|codex|cursor
muninn hook session-end|pre-compact --provider claude|codex|cursor
```



### Recovery command information

Use `--confirm-legacy-volume` only after Muninn reports a matching legacy macOS record without a disk ID.
The command repeats its checks before repair.
The result gives `recovery.status`, `message`, `repair_phase`, and `platform`.
Follow `next_action`.

Check service status with `muninn doctor --pretty`.
If the command reports `reboot_required`, stop and start the computer, then run it again.
If doctor reports a stopped service, start it through the system service manager.
Windows repair has no native qualification.

The rebuild operation keeps one maintenance owner and one acquisition deadline.
Full transcript passes use new connections in different writer regions.
Constant Git lookups that read no transcript material run between writer regions.
The writer is free and all material connections are closed during that work.
The `original` complete source/key/intent fingerprint is checked before each re-entry.

A concurrent writer, changed file identity or unsettled sidecar refuses as `source_changed`.
Only the database ctime change caused by the last writer's own private-permission normalization is tolerated in that same writer region.
Own replay that changes source bytes also refuses.
An unsuccessful or unreadable index `attempt` stays in the report after a subsequent pass that completes without an error.

On success `rebuilt` and `old_readable` are `true`.
`old_kept_as` names the kept archive.
`copied` holds checked table-copy counts.
`reapplied_tombstones` counts new intent rows applied to the candidate.

`ingest` holds invocation work totals as defined below.
`warning` explains archive retention.
`recovery_required` is `false` only for completed publication or refusal before uncertain mutation.

`publication_possible` becomes `true` before a replacement try and stays `true` on success or error.
A current orphan candidate also keeps it `true` because prior exposure is uncertain.
An unsuccessful replacement reports exit 4 `replace_failed` without an `old_restored` claim.
Refusals report exit 2 with a bounded `reason`.

Writer contention stays exit 3.
Check `recovery_required` for a busy result.

After each error, keep the actual live file, `tombstone.key`, current `tombstones.jsonl`, candidate, and each archive unchanged.
Muninn does not put data back, promote an orphan, retry, or infer permission to reenable admission.
A crash before publication can keep admission off.
A subsequent invocation must not guess its unrecorded prior value.

A missing answer or log does not prove nonpublication.
A new invocation validates current `state` again.
It refuses a current `muninn.sqlite.rebuild` or live `state` that fails identity or access checks.
A kept archive, with a copy that did not finish, is recovery `evidence`.
It gives no permission to overwrite current data.
Archive removal needs one more owner decision.

Erasure reports kept archives as `residue`.


Index counters describe work across the completed invocation.
`files_seen`, `skipped_files` and `unreadable_files` count visits or open `attempts` across passes, not different files.
`files_changed`, `events_added`, `events_removed` and `skipped_lines` sum committed work.
A `--full` retry stays full and counts its duplicate committed replacements.

`failed` and `errors` include ordinary unsuccessful read `attempts`, when a subsequent `attempt` succeeds for that source.
Missing Git lookup facts do not count as errors.
`missing` comes from the completed terminal pass, and `duration_s` includes Git lookups and lock waits.
A completed sweep can contain errors.

Each pass holds the writer independently.
Missing Git membership facts are checked while unlocked, then a new connection replans provider files and database `state`.
There is no total retry or runtime limit with continuously changing inputs.
Each Git probe keeps its ten-second timeout.

Git facts reflect when they were measured, not a coherent or current repository snapshot.
The poller publishes its completed heartbeat and clears its prior error only after the terminal pass.
Git lookups between passes can update liveness before the terminal pass completes without an error.

## Installing and removing (from a checkout)

| Platform | Installed desktop app `evidence` |
|---|---|
| MacOS | `Claude.app`, `Codex.app`, `Cursor.app` in `/Applications` or `~/Applications` |
| Windows | Desktop executables in `LocalAppData`/`Program Files`. Current-user `Claude`/`OpenAI.Codex` MSIX registration with a current executable declared in its manifest. |
| Linux | `claude-desktop`, `chatgpt`, `cursor` on PATH |

- Missing app: setup/config validation/installed-hook verification skipped.
- Installed Claude/Codex without config: manual setup guidance.
- Remaining settings/transcripts do not prove installation.
- History import and the current index do not use app detection as a prerequisite.

New installation finishes transcript indexing and available Cursor history before activating the release.
Its checklist reports the actual stored `event` counts by provider.
Unfinished ingestion or an erasure-cleanup error prevents activation.
Committed erasure stays in effect.

Recall starts off, and learning admission stays off until explicitly configured.
Service-start verification needs a new heartbeat after the completed initial index.

The installer checks for an interrupted pause before it prepares the database.
On macOS or Linux, it tries the checked repair path.
If repair succeeds, installation continues.
If repair needs a reboot, the installer stops.

Stop and start the computer, then rerun the same install command.
If Muninn cannot prove repair is safe, it keeps the repair files.
It stops the service and prints the next step and health-report command.
On Windows, it prints `support` steps and leaves the service stopped.

Windows repair has no native qualification.
The installer runs a last health check after service startup.

| Command | What it does |
|---|---|
| `bin/muninn-install` | Installs a new release or upgrades an installed release. See installer behavior below. |
| `bin/muninn-uninstall [--dry-run] [--purge-data]` | The same as `bin/muninn-install --uninstall`. See uninstall behavior below. |

### Installer behavior

The last install step moves each previous release to `.pruning-<ts>-<sha>` so an error can roll back.
A sweep removes those directories after rollback is no longer possible.
An unsuccessful sweep gives a warning and `sweep_failed` in the install record.
The next upgrade retries that sweep.

`muninn doctor` gives `release_leftovers` while a `.pruning-<ts>-<sha>` directory stays in `~/.local/lib/muninn`.
Those directories are in the release directory, not the data directory.
The installer invokes installed Python through its venv path, not the outer symlink.

If that runtime has no pinned dependencies, the installer uses a compatible checkout `.venv` again.
If necessary, it gets confirmation before making one from the hashed runtime lock.
On macOS/Linux, first install offers developer requirements and local `core.hooksPath` configuration to `.githooks`.
It does not replace a different configured hooks path.

`--check` previews without writes, package installation, or Git-hook setup.
It uses a compatible selected interpreter or complete checkout `.venv` again.
If the selected interpreter and checkout `.venv` are unavailable, it refuses and gives runtime-lock setup instructions.

`--status` compares the installed commit with `HEAD` using local Git history.
Equal revisions are up to date.
A newer checkout gives an upgrade preview.
A newer installed revision tells you to update the checkout before installation.

Unknown or divergent history reports `versions differ` and tells you to examine the revisions before installation.
Status does no fetch or installation.

### Uninstall behavior

The uninstall command stops the poller and removes the launchd plist.
It removes Muninn hooks from Claude's `settings.json` and Cursor's `hooks.json`.
It removes Muninn sections from Codex's `config.toml` and the Codex plugin cache.
It removes `~/.local/lib/muninn`.

It removes `~/.local/bin/muninn` only when the link points into that release directory.
It moves the data directory to `~/.local/share/muninn-removed-<ts>/muninn`.
`--purge-data` removes the data directory as an alternative.
`--dry-run` writes nothing.

The command prints one line for each step.
It ends with `uninstall done`, `dry run only: nothing was removed`, or `muninn is not installed here`, and exits 0.
If it cannot safely edit a provider configuration, cannot use the data-move name, or cannot remove the launchd registration, it prints `FAILED: <why>`.
That error exits 1.

Configuration and name checks run first, before changes, in a dry run too.

Upgrade policy previews keep the current writer lock while validating the same policy compatibility rules above a private, stable DELETE-mode store.
`review_policy_preview_unavailable` refuses a missing or busy lock, or a lock that fails privacy checks.
It also refuses an unaccepted header, journal/WAL/shared-memory sidecar, or changed snapshot.
The preview makes no lock, repairs no permissions and does no recovery or checkpoint.
Keep refused `state`.

A refused preview has not proven policy compatibility.
Use the recovery guidance when necessary.

The installer keeps that refusal code and appends one finite `reason`, for example `FAILED: review_policy_preview_unavailable: writer_busy`.
It does not print raw operating-system or SQLite errors.
The `reason` identifies the first measured check that was not satisfactory.
It does not identify a competing `process` or prove a permanent fault.

Refusal exits 1 and has not proven policy compatibility.

| `Reason` | Measured condition |
|---|---|
| `writer_busy` | The native cooperating-writer lock reported contention. Try the preview again after other commands finish. Contention can be transient. |
| `state_missing` | Necessary `state` disappeared or was unavailable. Keep the remaining `state` and examine installation health. |
| `unsafe_state` | A privacy or permission check did not satisfy the rules. The preview does not repair it. |
| `directory_changed` | The data directory metadata changed during the read. A heartbeat publication can cause this. The `reason` does not prove which operation changed it. |
| `lock_changed` | The lock identity or metadata changed. On POSIX, a competing writer can change lock metadata before its unsuccessful try to get the lock. |
| `database_changed` | The database pathname or held descriptor metadata changed. |
| `sidecar_present` | A journal, WAL, or shared-memory path existed at the sidecar check. Keep it. Do not remove it to bypass a preview refusal. |
| `unsupported_header` | The image had a short, invalid or non-DELETE SQLite header. |
| `sqlite_error` | SQLite could not open or query the guarded image. |
| `io_error` | A different operating-system read, identity, lock, or cleanup operation had an error. The `reason` gives no native error or specified operation identity. |

Some conditions can occur together.
An earlier privacy or metadata check can refuse the read before a sidecar check.
A service that runs does not by itself prove writer contention.
Do not bypass locks, remove sidecars, or change permissions to bypass a preview refusal.

## `muninn erase`

Select one session, `event` source locator, or literal match.
The match must have at minimum four characters after you remove adjacent whitespace.
Event-reference syntax is checked before coordination.
Missing or ambiguous `event` resolution happens in the governed database.
Unknown sessions keep their current zero-target/tombstone behavior.

`--match` searches `event` text and governed knowledge content, causes, expiry conditions, claims, `evidence` excerpts and unresolved content.
Erasure follows `support` and review-read associations through prior revisions, withdraws full affected guides, clears explanations and fences affected `requests`.
IDs, statuses and uncertain charge history can stay.
Removed text is not written to the call log.

Live erasure holds the owner barrier before the writer barrier.
It can stop the **full Muninn serve `process`**, and stop unrelated ingestion.
It waits for contained children, discarded `process` buffers, and completed cleanup before changing specified content.
It selects at maximum one owner.
A replacement produces `owner_changed` rather than a different stop `request`.

A missing database at this time reports exit 4, `store_unavailable` with `detail` equal to `store is absent`, without creating a database.
This differs from the older erase behavior.
Dry runs keep their writer-only, no-stop path and may initialize an empty database.
They make no `tombstone.key`.

A missing database proves no process-containment `evidence`.
An empty database proves no process-containment `evidence`.

POSIX review-worker shutdown keeps its owned, unreaped group leader while checking native shutdown `evidence`, before publishing an outcome or discarding `process` buffers.
Signals target only that checked `process` group.
On 64-bit Linux, all members across the `original` isolated session must be terminal, also those in other groups.
The kernel fork counter must stay unchanged across complete observations.
Darwin keeps its native sole-current-group zombie observation with `WNOWAIT`.

Same-session escape/rejoin and closed-session safety stay unverified.
This does not prove universal descendant `isolation`.
Unknown or unavailable `evidence` keeps cleanup pending.
Individual Linux observations have a work deadline.

Unrelated host forks or unavailable procfs can prevent cleanup from finishing.
`stop_all()` has a bounded join and reports `process_cleanup_incomplete` if cleanup stays pending, keeping ownership.

The Python APIs keep `run_erase` and caller-owned `erase`.
The latter needs proven pause and writer ownership for live erasure.
Callers needing observable status use `run_erase_request(home, EraseRequest(...), status=...)`.
The current `wait_s=15.0` default is unchanged.

For live erasure, this is one coordination-acquisition budget, with owner and writer waits.
It does **not** limit repair, physical I/O, or total command time.
`wait_s=0` tries to get the locks immediately, without publishing a stop, recording a repair baseline, or sleeping.
Writer contention raises Busy.
There is no new CLI timeout flag.

Each handled erase result, with mapped errors, adds `erasure_status`:

| Field | Meaning |
|---|---|
| `stop_may_have_been_published` | Conservative latch set before possible stop visibility. It does not say if the service runs or has stopped. |
| `repair_phase` | `none`, `baseline_recorded`, `reconciling` or `complete`, independently of the specified erase. |
| `erasure_phase` | `not_started`. `intent_may_be_durable` before the append. `intent_durable` after its durable report. `committed` after SQL commit. `verified` only after current checks complete with no remaining vocabulary, zero reported `residue` and zero kept archives. |

Only when a stop could have been published, status also contains `restart: "not_attempted"` and `next_action: "check_and_restart_if_needed"`.
Check the service and arrange a manual restart if needed, after an unsuccessful erase too.
Muninn does not automatically start the service again or reboot the host.
This status does not assert renewed service availability.
If containment is uncertain, an accepted boot observation can record a bound repair baseline.

The same boot refuses with `reboot_required`.
A subsequent accepted boot permits reconciliation only while all necessary bindings match.
Do not remove witness or repair-baseline files to bypass refusal.
Windows boot repair and retirement of a current repair baseline stay **unaccepted required-platform release blockers**.

Usual valid-complete ownership without a baseline uses a different path.
Refusal is not completed Windows recovery `support`.
Native containment and boot `qualification` stay more release checks.

Each new macOS repair baseline keeps the data volume's permanent UUID.
After a reboot, the device number can change.
Muninn accepts that change only when the UUID, lock inode, witness inode and witness digest match.
If the UUID is unavailable, Muninn does not record a new baseline.

A legacy baseline has no recorded volume UUID.
It cannot approve a device-number change automatically.
A matching active-owner legacy path needs explicit disk confirmation and the subsequent-boot checks.

Write-ahead tombstones survive interrupted erasure and are replayed before ordinary paused startup/erase entry.
Pending intent refuses new foreground proposals and commits.
A current receipt has receipt-first audit access until replay scrubs its governed explanation.
The journal guard reads the full log with no constant byte, memory, or time bound.

Keep the current journal and durable `tombstone.key`.
Without the key, keyed tombstones cannot be checked.
`Event` content tombstones are keyed HMACs of role/text and apply only to fork replay, not to the same text in a subsequent turn.
Current family copies are erased conservatively: all matching ancestor `events`, and only the replayed leading run of descendants or siblings.

A historical erasure without a content tag can suppress a byte-for-byte line copy.
A differing copy is not recognized by that line tombstone.

Erasure also traverses memory candidates, each accepted decision revision, locator-only pending decisions, replacement dependencies and application `requests`.
It clears candidate payloads, decision locators and affected `evidence` excerpts.
An erased candidate cannot be displayed or applied.
Historical receipts and structural IDs, revisions, digests and accepted timestamps stay for audit.

Current `evidence` keeps correct provenance metadata, with source paths and selectors.
Those open strings are not a universal content-free guarantee.
Provider transcripts and kept archives stay unchanged.
Replay and archive-restoration refusals prevent those archives from silently putting back erased governed bodies.

The `review` counts include removed working files and `retained_archives`.
Only program-owned `review-work/<AttemptId>` material is removed after containment.
Symlink targets, unrelated siblings and provider transcripts are kept.
`vocab` and `residue` report the current bounded-needle checks.

`verified` is **not universal forensic proof that all text is gone**.
It gives no guarantee about remote/provider copies.
Kept archives keep the phase `committed` when no selected needle matches.
Commands that finish with `residue` report exit 0.
`aside_files` lists kept unreadable, recovery and pre-upgrade stores.

`aside_remove` gives the literal manual removal command (`rm --` on POSIX, `Remove-Item -LiteralPath` on Windows).
These archives are not removed automatically.
Report `provider_files`, `not_covered` and `out_of_scope`, with recovery copies and free filesystem blocks.

Current mapped exits stay: Busy 3.
Hot journal, tombstone key, unavailable database or filesystem access 4.
Value/lookup refusals 2.
Status keeps the latest reached milestone and safe current error information.

Arbitrary SQLite errors propagate.
No output is guaranteed after `process` death or output error.

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

`review` is a schema-1 `ReviewStats` object.
`requests_by_state` and `calls_by_state` count durable rows.
Missing `states` have 0 rows.
`oldest_pending_age_seconds` covers pending/waiting `requests`.

`last_progress_age_seconds` measures the newest durable `attempt` `progress`.
Ages are nonnegative, or null when there is no matching row.
`unresolved_items` counts open items.
`usage` sums actual per-call reports one time.

Unknown components stay null.
Kept `usage` does not resolve an unconcluded call.
Reservations do not become measured costs.

Mixed, unknown, or invalid currencies keep cost and currency null.

Diagnostic currency must satisfy `[A-Z]{3}`.
`--usage` reports transcript-observed CLI `usage` independently of worker `usage`.

`admission.enabled` reports the stored admission switch as a boolean.
It is null when there is no correct singleton control.
`admission.policy_status` is one of `control_absent`, `control_invalid`, `policy_absent`, `policy_invalid`, `policy_digest_mismatch`, or `valid`.
`valid` needs the checked policy, its current projection, and a matching canonical digest.

An enabled, correct control reports stored prerequisites only.
It does not prove provider access, scanning, or learning success.
These observations do not change admission or repair `state`.

`eligible_records` and `completed_records` count currently indexed logical prompt/reply records, deduplicated by provider, thread and physical line.
They are not absolute source cursors.
They are not lifetime counts of erased or removed data.
Activation archive records are not counted unless explicitly admitted for historical `review`.
Overlapping admitted windows count one time.

Only success receipts can count completed work.
These include no-change receipts.
Each needs current indexed provenance.
The `request` endpoint must have the same hash as its admitted hash.
That hash proves only the endpoint.
Records between the endpoints need matching live `evidence` linked to the same `request`.

Missing/changed/erased input or unprovable provenance stays an uncertain `gap`, reducing metric availability without changing historical audit receipts.
No `original` transcript is read by health, so these are indexed observations rather than live physical validation.
A subsequent explicit receipt can count work while an earlier `gap` stays.
Historical replay does not change the activation baseline.

Durable `states`, receipts, and `gaps` survive log-file replacement or disabled logging.
Per-source reconciliation refusal causes are not durable.

`observed_enqueue_busy` counts only kept `review_enqueue_busy` log records (null when no readable correct log record stays).
`busy_count_complete` is always `false`.
A count of 0 measured busy records does not prove that all acknowledgments succeeded.
Subsequent recovery can capture indexed content, but cannot prove the unrecorded lifecycle `event` or cutoff.

Cursor recovery waits for the next native import or explicit full index.
Put governed explanations only in explicitly specified `learn show` output.
Do not put them in health output or logs.
An `active` knowledge `state` alone does not prove current eligibility.

## `muninn doctor`

Answer: `ok` (`true` when no error-level check is `false`) and `checks[]`.
Each check has four fields:

| Field | Meaning |
|---|---|
| `check` | The check's name. |
| `ok` | `true` means the check passed. `false` means it was unsuccessful. `null` means it could not be checked. |
| `level` | `error`, `warn`, `info`, or null. An error-level `false` makes the full answer `ok: false` and exit 1. Warnings and information do not make the run unsuccessful. A null level means unverified. |
| `detail` | A short string with the meaning given below. Empty when there is nothing to add. |

A null service result does not prove that the service runs.
On macOS, `launchd_job` reports constant codes when observation cannot check the `process`.
The codes are `launchctl_unavailable`, `launchctl_pid_unavailable`, `process_inspection_unavailable`, `process_inspection_timeout`, `process_inspection_failed`, and `process_pid_unverified`.
The other checks stay in the report.
A launchctl timeout reports `ok: false` with `launchctl_timeout`.

A different query that did not complete reports `ok: false` with `launchctl_failed`.
A query that did not complete alone does not prove that the service is stopped.
Native messages and `process` command lines are not reported.
Each native command has a 60-second limit.

An unavailable observation does not make a recovery step necessary.
Exit 0 means only that no error-level check is `false`.

When a repair baseline exists, the result includes `recovery_guidance`.
It gives `platform`, `status`, `reason` and `steps`.
`review_repair.detail: baseline_present_recovery_unverified` means only that a repair-baseline path exists.
Its record and containment safety have not been checked.

On macOS or Linux, `status: recovery_check_required` with `reason: repair_baseline_present` asks you to run the checked recovery command.
This is not a completed repair or a promise of automatic repair.
Windows and unaccepted platforms report `status: manual_recovery_required`, with `reason: windows_repair_not_qualified` or `recovery_platform_unqualified`.
Doctor does not open or validate repair records.

Malformed paths or paths that fail access checks also make checked recovery necessary.
Recovery can refuse without repairing them.
On macOS or Linux, run `muninn recover`.
It uses the owner and writer barriers.
It repairs pause records only when the recorded boot and file identities match.

During an upgrade, the installer has one confirmation step about a matching older macOS record without a disk ID.
Continue only if this is the Muninn data you intend to repair.
The installer repeats its checks before repair.
For direct recovery, run `muninn recover` first.

If it reports `legacy_volume_confirmation_required`, run `muninn recover --confirm-legacy-volume` only for the correct Muninn data.

Completed macOS supervisor records with `volume_uuid` can survive a device-number change when the native volume UUID and lock inode match.
The command keeps the private-file, ownership, descriptor and writer-lock checks.

A legacy completed record has no disk UUID.
If only the device number differs, `recover` reports `legacy_volume_confirmation_required`.
Check the intended data directory before using `--confirm-legacy-volume`.
That confirmation supplies disk identity the legacy record cannot prove.

Recovery repeats the record, inode and repair-fingerprint checks while it holds the owner lock and writer lock.
It applies recorded tombstones before it writes the current identity and retires the repair baseline.
This completed-owner path needs no new boot.
An active owner, a changed record, or a different recorded UUID cannot use this path.

Before runtime selection, the installer keeps supervisor output in the legacy format.
The selected runtime adds the UUID when it starts.
If an upgrade fails after selection, rollback keeps the selected runtime and leaves the service stopped.

A previous runtime can reject `volume_uuid`.
Do not manually select a release that cannot read the current supervisor record.
A SQLite copy does not include this JSON record.

If it reports `reboot_required`, restart the computer.
Then run `muninn recover` again.
After recovery, run `muninn doctor --pretty`.
Doctor does not stop a service or change recovery files.

On Windows, `muninn recover` refuses before it changes repair files.
Windows boot identity and safe baseline retirement have no native qualification.
Keep the data folder and all repair files.
Keep the doctor report.

Contact the maintainer.

The owner must do three steps.
Stop and start the computer when Muninn says a new boot is necessary.
Start the service if doctor reports that it is stopped.
Contact the maintainer for Windows or identity-mismatch refusals.

Muninn does not reboot the computer because a reboot interrupts other work.
`recover` does not revalidate the installed service definition before it recommends a service start.
A changed or unowned entry could run unexpected code.
If identity checks fail on macOS or Linux, Muninn keeps the files.

It tells you to contact the maintainer because it cannot prove which `process` or database owns those files.

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
| `upgrade_snapshot` | `warn` | No kept pre-upgrade recovery archive exists. | Names of kept `muninn.sqlite.pre-upgrade-*` files. Upgrades without an error keep them. Keep them until the owner checks recovery and retention needs. They can contain transcript text and a legacy knowledge ledger that `erase` cannot scrub. Do not remove them only to clear this warning or replace newer data with them. |
| `tombstone_key` | Error | `tombstone.key` has its full 32 bytes and exists when keyed tombstones exist. | `missing` or `damaged`. |
| `release_leftovers` | `warn` | No `.pruning-*` release directory is left in `~/.local/lib/muninn` | Their names |
| `launchd_job` | Error | On macOS, launchctl reports one positive PID and a completed PID-only `process` query agrees. Unavailable inspection reports `null`, not success. | Checked PID or a constant observation code above. |
| `managed_service` | Error | On Linux/Windows, the managed user service/task is owned and its heartbeat identifies the actual writer. Unavailable inspection reports `null`, not success. Paths compare by resolved identity. Each command has a 60 s limit and a timeout makes the check unsuccessful. | Constant code and `process` ID. |
| `roots_readable` | Error | Each provider root that exists is readable | Names of blocked roots |
| `roots_present` | Info | Always. | Names of provider roots that are not on disk. |


`Review` diagnostics also use the same four-field check contract:

| Check | Level | Meaning of `detail` |
|---|---|---|
| `review_backlog` | Info | Durable pending/waiting `request` count |
| `review_policy` | `warn` | Installed durable policy is configured, or missing/invalid while admission is on. |
| `review_admission` | Info/`warn` | Recorded admission and checked policy integrity. `open` or `closed`, a policy refusal code, or unknown control metadata. |
| `review_auth`, `review_config` | `warn` | Attempt-history counts with those finite error codes. Invalid stored `attempt` policies count as configuration errors |
| `review_process_deadline` | `warn` | Recorded deadline errors, or unfinished `attempts` after their explicit policy deadline with unended calls. This proves no live `process` or completed kill. |
| `review_commit_delay` | `warn` | Overdue `running` `attempts` without an unended call, awaiting durable finalization. |
| `review_cancel_delay` | `warn` | Overdue cancelled `requests` with unfinished `attempts` |
| `review_abandoned` | `warn` | Recorded abandoned `attempts`, or unfinished `attempts` from an older generation |
| `review_coverage_gaps` | `warn` | The same eligible set as stats, with admitted historical replay, without success receipts with current matching provenance. The untouched activation archive stays excluded. |
| `review_capture_gaps` | `warn` | New indexed records after the eligible cursor, or eligible records without checked prior admission provenance, after refusal or log loss. |
| `review_pause` | Info | The witness file exists. Always null because containment is unverified. |
| `review_repair` | `warn` | A repair-baseline file makes recovery guidance necessary. Do not remove the files. |
| `review_erasure_intent` | Info | A journal exists, but its consistency is unverified. The file does not prove an outstanding intent. |
| `review_windows_repair` | `warn` | Windows boot repair and durable repair-baseline retirement stay unqualified |

`review_policy` keeps its configuration-only meaning.
It does not check the policy digest or prove that admission is open.
`review_admission` reports `open` with `ok: true` only when the switch is on and the checked policy has a matching canonical digest.
A closed switch is informational with `ok: false`: `closed` for a correct policy, or `closed_policy_absent`, `closed_policy_invalid`, or `closed_policy_digest_mismatch`.

An open switch with `policy_absent`, `policy_invalid`, or `policy_digest_mismatch` produces a warning.
A missing or invalid singleton produces `control_absent` or `control_invalid`, a warning with `ok: null`.
These checks do not prove provider access, scanning, or learning success.
`review_config` counts historical `attempts`.

It does not by itself prove a current admission error or authorize a recovery step.
With no control row, `review_abandoned` is unknown with `generation_unavailable` and `review_capture_gaps` is unknown with `control_absent`.
The two results are warnings.
The history, backlog, coverage, and required-input checks stay available independently.

Capture-gap counts do not identify refusal causes or prove unrecorded lifecycle `events`.
New hook, catch-up and `attempt` logs keep `evidence_limit` when source verification exceeds its shared byte, record or database-work allowance.
These limits include source traversal, not only selected text.
Older capture logs can contain only `unavailable`.

Their specified cause is unknown.
Do not increase limits or replay history only to clear a count.

If the report has `recovery_guidance`, check service status.
If Muninn is stopped, leave it stopped until recovery is complete.
Keep the data folder and all repair files.
Keep the full report and contact your Muninn maintainer before a new service start.

Doctor does not initialize, reset or remove supervisor/repair records.
Missing DB, missing PID or free locks do not prove containment.
Keep `review.supervisor.lock`, `review.supervisor.json`, `review.supervisor.repair.json`, `tombstones.jsonl`, keys and kept archives.
Do not remove protocol `evidence` to bypass a refusal.

Erase can stop the full service.
Examine its current `erasure_status` and `restart: not_attempted`, then check and restart manually if needed.
A kept archive prevents checked erasure with no matching selected needles.
Windows uncertain-state repair and baseline retirement are required-platform release blockers.

Satisfactory fakes or ordinary operation without a baseline do not prove native `qualification`.
Original-source read budgets do not bound erasure-journal work, total I/O, or command time.
`Process` deadlines do not bound DB commit or cancellation that stays pending.

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

This read-only startup context does not make a learning policy necessary.
No arbitrary eight-item limit or actor/quote heuristic stays.
Muninn omits items that do not fit and reports that omission.
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
  `last_error` and `last_error_at` identify the class and time of the last pass with an error.
  The next good poller pass clears the two fields. Manual `ingest` does not clear them.

  Muninn refreshes `alive_at` at intervals of a small number of seconds during a pass.
  A long classifier re-read can thus show `poller` `ok` while `index_age_s` increases from the last completed pass.
- `calls.jsonl` and `calls.jsonl.1` have one allowlisted line for each CLI call: IDs and counts, with no text.
  The log moves to its numbered file at 1 MiB.
- `poller.log` and `poller.log.1` have poller `events`, with the same 1 MiB limit.
  The plist sends launchd stdout and stderr to `/dev/null`.
  If the plist sends them to the log, the poller silences them itself.
  Only allowlisted lines reach the log.

  Launchd starts the poller again after each exit, with a clean stop included.
  Only `launchctl bootout` keeps it down.
- `tombstones.jsonl` has erase records.
- `tombstone.key` has 32 random bytes and mode 0600.
  Muninn makes the complete key the first time an erase makes it necessary.
  A dry run does not make the key.
  The key survives `rebuild`. Muninn does not write the key to logs.

  It makes content tombstones in `tombstones.jsonl` keyed HMAC-SHA256 tags (`ev2:`), not plain hashes.
  This prevents an attacker from testing guesses of erased short text against public hashes.
  Muninn does not make a new key while keyed tombstones exist: a new key prevents matching.

  If the key is unavailable or damaged, `ingest`, `rebuild`, and `erase` exit 4 with `tombstone_key`.
  `doctor` fails `tombstone_key`.
  Put the file back from a recovery copy.
- `muninn.sqlite.unreadable-*` are historical unreadable copies.
- `muninn.sqlite.recovery-*` are kept rebuild archives, with copies that stopped before they finished.
  `erase` cannot scrub these copies, which can hold erased text.
  It lists each file in `aside_files` and gives its literal removal command in `aside_remove`.
  Commands are `rm -- <path>` on macOS/Linux and PowerShell `Remove-Item -LiteralPath` with a literal path array on Windows.

  `doctor` warns with `aside_files` while one of these files exists.
  Muninn does not remove it automatically.
  Salvage necessary data, then run the given removal command.
- `muninn.sqlite-journal` is SQLite's rollback journal.
  `unexpected_files` allows it, but a remaining journal fails `unowned_journal`.
- `recall-review.sqlite` keeps private `delivery` metadata and `review` checkpoints.

  `recall-review.sqlite-journal` is its transient SQLite journal.
  These files keep no transcript text.
- `recall.off` and `recall.advisory` select prompt retrieval mode. Each must have mode 0600.
- Kept `muninn.sqlite.pre-upgrade-<sha>` archives have mode 0600 and are erasure `residue`.
  Their files give no automatic permission to put data back.
- `review.supervisor.lock` is the unchanged lifetime lock.
- `review.supervisor.json` is the private completed-owner witness.
- `review.supervisor.repair.json` is the private uncertain-owner repair baseline.
- `stop.json` targets one selected service generation.

Other permitted learning files include `review-capabilities`, `review-prompts`, and `review-runner-cwd`.
The list also includes `review-codex-schema.json` and the versioned `review-policy-codex-ongoing*.json` files accepted by the installed code.
These are known protocol files, not cleanup instructions.
Unknown files fail `unexpected_files`.
On Windows, heartbeat and stop metadata publication retries native access-denied or sharing-violation errors (5/32) for at maximum one second.
A reader held longer causes refusal and keeps the previous `state`.

## Native provider commands

The installer makes hook JSON with native paths rather than inserting raw paths into JSON text.
On Windows, Claude uses a native Windows PowerShell executable with an `args` array.
Codex uses its `command_windows` override with an encoded PowerShell command.
They invoke the validated Muninn bootstrap without Git Bash.

Codex SessionEnd uses a three-second configured timeout.
The other Claude and Codex hooks use five seconds.
Changing a hook definition needs the owner's provider approval.

Windows installer calls prefer a trusted `codex.exe` on `PATH`.
An official npm `codex.cmd` installation is sent through its current `node_modules/@openai/codex/bin/codex.js` with a native `node.exe`.
The installer sends arguments directly.
Other shim layouts need putting native `codex.exe` on `PATH`.

Executables or provider configurations that fail access checks are refused.

Automatic Codex hook trust needs a correct version command: `codex-cli 0.159.2` on Windows, or `codex-cli 0.159.2`/`codex-cli 0.159.3` on macOS and Linux.
The selected CLI files and version must stay unchanged from discovery through the check before trust is written.
If not, the installer leaves trust to the owner in `/hooks`.
Measured Codex `0.162.1` is not admitted by this older version allowlist without new `evidence`.

Bounded compatibility observations are not native `qualification`.

Repository scopes use local native filesystem paths.
Foreign-platform absolute paths and Windows UNC paths resolve to `unknown` without probing the network.
Current scope ids are kept.
Copy network-hosted inputs locally before indexing.

### Renew an approved Codex computer binding

This procedure needs an installed current-format 2 database and macOS ARM64.
First resolve current containment or erasure recovery.
A marked format-1 install must complete its ordinary checked upgrade first, with no renewal or new policy flag.
That prerequisite keeps the recorded computer policy and grants no new computer permission.

Do not do migration and renewal together.

The agent prepares an explicit candidate from the recorded policy.
Only `experimental_codex.host_identity` can change.
Keep the `original` policy source, account, authentication home, approval, projects and each limit.
Preview it:

```sh
bin/muninn-install --upgrade --review-policy PATH --renew-codex-host preview --check
```

The preview writes nothing.
`--check` works in all argument positions and runs no runtime setup, Git-hook setup, or approval prompt.
Apply the unchanged displayed plan:

```sh
bin/muninn-install --upgrade --review-policy PATH --renew-codex-host PLAN_DIGEST
```

The installer needs a new interactive `yes` or `y` after its full disclosure.
The digest alone, a previous conversation, or an enable-learning flag is not consent.
A changed plan needs a different preview and approval.
New installs, scope changes, enable-learning and unrelated policy changes are refused here.

This is a full upgrade.
It starts Codex app-server initialization and hooks/list checks, and background learning can start before it reports.
Ongoing permission has no aggregate call or spending cap of its own.
Current policy limits stay.

Provider billing and full `isolation` have no native qualification.
Containment can refuse.

The renewal keeps previous `request` identities and immutable policy snapshots, charges, receipts, source `progress`, scope and activation time.
It does not replay previous work or reopen paused learning.
An independently approved history operation can select a previous window subsequently.
A candidate digest found in a kept `request`, with terminal `requests`, cannot be used again.

Duplicate computer changes can thus make maintainer `review` necessary rather than a different renewal.

Policy COMMIT is the grant boundary.
A subsequent restart or verification error reports `host approval committed; upgrade incomplete`.
Run again only the same approved upgrade to continue its kept receipt.
Do not put back an archive, rewrite a receipt, grant again or reopen admission to work around a refusal.

An unexplained installer-lock metadata `gap` has no automatic continuation.
The agent reports the refusal unchanged and keeps the record.
An operator or maintainer must `review` the `evidence` before independently accepted recovery.
### Follow the current trusted Codex installation

The managed interface mode follows the local Codex manager on Apple silicon macOS.
It uses `~/.codex/packages/standalone/current` and confines the target to `releases`.
It checks the complete package, the primary Apple signature, and the advertised JSON `review` arguments.
It does not select an executable from `PATH` or a project folder.

Compatible Codex updates keep the same policy digest, account, scope and limits.
Versions and hashes identify each actual call.
They do not grant permission.

A previous runner approval with fixed identity keeps that same identity requirement.
A different executable hash refuses regardless of a correct Apple signature.
The installer has one explicit procedure to replace that previous selection with a stable interface contract:

```sh
bin/muninn-install --upgrade --codex-interface preview --check
bin/muninn-install --upgrade --codex-interface PLAN_DIGEST
```

The preview writes nothing.
It checks the recorded previous approval, auth directory and schema without needing the previous executable.
It checks the current interface with only `--version`, `--help` and `exec --help`.
These checks use a fixed Darwin sandbox and a read-only environment with no auth configuration.
They run no model task.

Missing or denied `isolation` refuses.
There is no uncontained fallback.

The apply command needs a new `yes` or `y` for the unchanged displayed UPDATE RULE.
A preview, digest or source `review` is not that consent.
Ordinary upgrades do not enroll this rule.
Do not use it with new install, host renewal, `--enable-learning`, new history/scope, or a different policy input.

This one approval also enrolls this physical computer.
The previous name-based digest can change when the computer name changes.
It does not prove which physical computer held the previous approval.
The preview reports that continuity as `unverified`.

It shows `saved_legacy_host`, `observed_legacy_host` with the label `observed at preview`, and typed `target_host`.
These fields grant no permission when you read them.

The new terms use `ongoing-mac-codex-managed-v1`.
The physical target has type `darwin-ioplatformuuid-v1` and a digest.
Muninn reads the public Darwin physical ID one time, with a positive native wait of at maximum 50 ms.
A shorter caller deadline reduces the wait.

Operating-system waits can increase total elapsed time.
Muninn keeps no raw ID or computer name.
Hashing does not prove protection of the computer or user `process`.
Hardware repair and copied virtual machines can change the identity.

A computer-name change on the same physical target keeps the grant.
`codex_host_changed` means a checked physical target differs.
`codex_host_identity_unavailable` means Muninn cannot read a checked physical ID.
Each condition stops native setup or checked restart.

There is no identity fallback.
`codex_interface_legacy_approval_invalid` means the previous proof, prompt, schema, settings or auth directory cannot `support` the proposed change.
Repair that cause before a new preview.

The plan, bridge and approval keep the `original` preview diagnostic.
Apply checks each other approved field.
Before grant publication, SQL commit and service start, Muninn checks the physical target and selected runtime.
The runtime must declare `codex-managed-host-enrollment-v1` and parse the new terms.

A previous selection marker alone cannot pass.
A selected runtime that cannot read the terms stops with `codex_interface_runtime_required`.
After commit, Muninn keeps the new grant stopped for checked recovery.
It does not put back a previous grant or ask for the same approval again.

The rule trusts the owner's local manager.
Local metadata and hashes do not prove publisher identity for each helper or resource.
Same-user compromise and unknown billing information stay limits.
The rule keeps the current auth home, model, `reasoning`, budget account, permissions, projects, history, consumed `attempts` and charges.

It introduces no API or billing path.
Generic Linux, Windows and Claude runner behavior is unchanged.

The initial operation checks the complete eligible previous queue before commit.
Its one finite capsule keeps immutable `request` snapshots and `original` inputs.
It refuses unaccepted pending decision work or a changed queue.
It does not cancel or recreate previous `requests`.

New inputs need the usual source, scope and erasure checks.
Future compatible Codex updates need no capsule or new approval.

Each managed `attempt` records its stable `codex_managed_interface_v1` selection.
Each call's `requested` is a `codex-managed-call-v1` record.
It contains that selection, up to two checked candidates, activation offers/refusals, one refresh count and a write-once `launched_identity`.
An offer does not prove a launch.

Native `observed` fields stay null when Codex did not report them.
Completed `rounds` keep their own actual versions and hashes after a subsequent update.

Before native execution, one proved manager race can replace a candidate with the same call reservation.
It cannot make a different charge or model task.
A second race stops with `runtime_race_exhausted`, without one more automatic `attempt`.
Started or uncertain calls are contained through the current rules.

A missing output or a resource error after execution does not authorize relaunch.

Observation caches in `review-capabilities/managed-cache` are disposable metadata.
Their key binds actual package/file identity, hashes, recipe, schema and validator revision.
Deletion or safe invalid content needs new isolated `qualification`, not new consent.
Cache storage that fails privacy or identity checks refuses.

A preview does not repair a cache.
Authority and bridge loss cannot reconstruct consent.
History estimates report `runtime_status: unknown` without a checked current runtime.
Only actually launched comparable identities contribute samples.

Codex-only structural `bounds` are 384 MiB for each file, 512 MiB for each package, 256 inventory paths and depth eight.
Cache and initial bridge files are at maximum 256 KiB.
The initial bridge contains at maximum 1,024 unique `request` IDs.
These `bounds` do not change model input/output, time, read, worker or `attempt` limits.

Generic executable verification keeps its 256 MiB bound.

The installer keeps a typed `codex-managed-interface-receipt-v1` with a minimum runtime requirement.
It records approval, inert artifacts, unchanged commit intent, the committed grant, and completed cleanup.
A crash before the commit keeps previous selected `state`.
A crash after SQL commit recognizes only the same successor, without replaying the commit.

After the durable committed phase, usual generation and `progress` changes do not invalidate permanent bridge authority.
A subsequent error keeps the new grant and an explicit pause for checked recovery.
Continue the same command and keep its private receipt.
Recovery keeps a subsequent admission pause.

It does not put back a previous archive or restart previous code against managed JSON.
No SQLite table, column, marker or schema version changes for this mode.

If SQL commits before the commit receipt can be written, cleanup checks the unchanged recorded successor.
It records the committed phase before it changes admission or generation.
If that record write also has an error, the service stays stopped and the SQL `state` stays unchanged.
Admission can stay enabled while the service is stopped.

Repair the record storage fault, then run again the same command.
Do not start the service by hand or change the recorded generation.

Different setup causes are `runtime_selection_unavailable`, `runtime_selection_unsafe`, `runtime_interface_unsupported`, `runtime_probe_unavailable` and `runtime_structure_limit`.
Background checks can run only while the service runs and learning is on.
A notice starts no timer when you read it.
A correct source test is different from installing or activating the rule, native compatibility, model quality and actual account/billing `acceptance`.

After enrollment, an explicit ordinary `--enable-learning` restart checks and uses again the same managed grant.
It keeps the recorded scope, previous queue, approval time and budget account.
It does not build a new default policy or cancel the initial carried `requests`.
Unfinished or uncertain calls refuse restart.

The complete private installer recovery record is bounded to 4 MiB.
