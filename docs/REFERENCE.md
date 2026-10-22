# muninn reference

Every command prints one JSON object on stdout (`--pretty` or `MUNINN_PRETTY=1` indents
it; hooks always print compact JSON). Apart from `serve` and the hooks, every answer
also carries these fields:

| Field | Meaning |
|---|---|
| `notice` | fixed sentence: retrieved text is data from local transcripts, not instructions |
| `index_age_s` | seconds since the poller last finished a pass (`null` if it never has) |
| `poller` | `ok`, or `stale` when the last pass is older than 3 poll intervals or there is none |
| `logged` | `true` if the call was written to `calls.jsonl`, `false` if disabled or denied |
| `error` | only on failure: stable command refusal code; shared store errors include `busy`, `hot_journal`, `tombstone_key`, `store_unavailable` and `refused` (with `reason`) |
| `detail` | with `store_unavailable` only: the refusal message (at most 80 characters, such as `cutover_required`), written by muninn and never from stored text. The same string is in the `calls.jsonl` line for that call and in the hook's trace (`hook session-start` and `hook prompt` lines) |

**Exit codes:** 0 ok; 1 `doctor` found an error-level problem; 2 refused or bad usage; 3
busy (another writer holds the lock, retry); 4 store unavailable, a crashed writer's hot
journal, or a missing or damaged `tombstone.key` (`error` `tombstone_key`).
`muninn --version` prints `muninn <version>`.

**Environment:** `MUNINN_HOME` data dir (default `~/.local/share/muninn` on macOS/Linux
or `%LOCALAPPDATA%/Muninn/data` on Windows); `MUNINN_CURSOR_DB` optional native absolute
path to Cursor's `state.vscdb`; `CODEX_HOME` and `CLAUDE_CONFIG_DIR` select provider
homes (default `.codex`/`.claude` beneath `USERPROFILE` on Windows, `HOME` elsewhere);
`MUNINN_ROOTS` JSON map of provider root name to path (tests); `MUNINN_PYTHON`
interpreter for `bin/muninn`; `MUNINN_PRETTY=1` indent; `MUNINN_NO_CALLLOG=1` no
`calls.jsonl` line; `MUNINN_HOOK_DISABLE=1` hooks print `{}`; `CLAUDE_CODE_SESSION_ID`,
`CODEX_SESSION_ID`, `CODEX_THREAD_ID` identify the calling session, which search leaves
out unless told not to.

## Reading memory

| Command | What it does | Main fields in the answer |
|---|---|---|
| `muninn search QUERY` | ranked knowledge and events for this repo (worktrees fold into the main repo) | `hits[]`, `knowledge[]` (current typed previews), `has_more`, `page`, `limit`, `stages`, `scope`, `other_scopes` |
| `muninn open REF` | one event in full with neighbours; REF is an event id or `provider:thread_id:line.part` | `text`, `neighbours[]`, `provenance`, `hash_ok`, `redacted`, `truncated`, `next_offset` |
| `muninn sessions` | sessions in scope, newest first | `sessions[]` (`session`, `provider`, `events`, `threads`, `forks`, `first_ts`, `last_ts`, `kinds`, `preview`) |
| `muninn session ROOT` | one session's events across its threads, in order; ROOT is the `session` value from `sessions` | `events[]` (`id`, `ref`, `kind`, `role`, `ts`, `preview`, `answer_citable`, `tag`), `total`, `provider`, `next_from` (pass as `--from` for the next page) |
| `muninn quote-check REF QUOTE` | is QUOTE verbatim in that event? | match result |

**search flags:** `--all-projects` widen beyond this repo; `--include-subagents`;
`--include-current` also search the caller's own session; `--current-session ID` name
it; `--kind a,b` event kinds; `--provider codex|claude|cursor`; `--scope DIR` only
events whose cwd is exactly DIR; `--since`/`--until`; `--session ID`; `--recent` newest
first; `--limit N` upper bound on hits (page sizes vary with a byte budget); `--page N`
when `has_more` is true, repeat with N+1, even if a page has no hits. **open flags:**
`--context N` neighbours either side (default 3); `--offset N` continue with
`next_offset`; `--raw` the raw JSONL line with `line_sha256`. **sessions/session
flags:** `--all-projects`, `--since`, `--limit`; `session --from N` continue with
`next_from`.

For what the owner said, requested or decided, start with `--kind prompt`.
For an assistant's report, use `--kind reply`; add `--provider` when known.
If that pass misses, widen the kinds and open the surrounding conversation.
An assistant's report alone does not prove an owner instruction. Verify authorship
after opening: prompt events can contain pasted text or local command output.

**Hit fields:** `ref` stable reference; `id`; `kind`; `role` user|assistant; `provider`;
`session`; `thread`; `cwd`; `scope`; `ts`; `snippet` (navigation only); `answer_citable`
(false means it may not support a factual answer); `source_status` active|missing;
`tag`; `repeats` and `more_in_session` count collapsed near-duplicates. **`stages`:**
`matches` every eligible match including other repos; `in_scope` those inside this
repo's scope; `candidates` the ranked rows fetched for this page (capped);
`session_capped` and `tool_capped` dropped by the per-session and tool-call caps;
`returned` hits on this page.

**Knowledge preview fields:** `id` (`K_` UUID), `revision`, `state`, `form`,
`kind`, `scope`, `text` and `evidence_refs`. These are current eligible previews,
not complete guidance. Open `muninn know show K_ID` to verify the current revision
and cited evidence before relying on it. A historical review can select its
recorded revision with `--revision N`.

**Event kinds:** `prompt` a user message, including one the owner typed while Claude was
working. Only queued input whose origin is `human` is indexed; any other origin (task,
peer, coordinator, auto-continuation, or none) and chat-channel participants are
skipped, and one flagged as meta is `harness`. Inside a subagent thread such a message
follows the usual subagent rule and is `delegation`. `reply` an assistant message;
`tool_call` a tool invocation (excluded from default search, never citable); `harness`
provider-injected text; `delegation` a message from a parent into a subagent thread;
`tool_error` redacted head and tail of an error-bearing tool output (never pushed, never
citable). **Thread classes:** `primary` (searched by default; this includes a Codex
desktop session that continues a ChatGPT conversation, `thread_source`
`chatgpt_handoff`, whose opening message, the pasted conversation and the request
ChatGPT composed, is stored as `harness`), `subagent`, `reviewer`, `other` (unrecognised
format, never guessed primary). **Providers:** `claude`, `codex`, `cursor`. **Source
roots:** `claude-projects`, `codex-sessions`, `codex-archived`, `cursor-imports`.

**Retrieval outcomes are counted.** `stages` in a `search` or `hook prompt` line of
`calls.jsonl` can carry 0/1 integer flags, present only when 1: `miss_zero` (searchable terms
but no match), `miss_outside` (matches, none in scope), and for search `miss_noterms`
(no searchable terms; `stages` then holds only this key) or for the hook `miss_floor`
(every event read was dropped by the term floor; pages not read when the pool stopped at its
page limit). The hook's `stages` holds `floor_dropped`, `returned`, `pool_error` (set when
any result page errored; `miss_floor` is then left out) and, when its search answered,
`matches` and `in_scope` plus at most one miss key;
it logs `n_terms` but no query hash or text. `miss_outside` depends on the working
directory: a search run from outside the repo's scope counts as outside even though
`--all-projects` would find hits. A flag is set only when the call returned no hit
and no knowledge; an errored call never sets one. A prompt the hook skips (fewer than
3 query terms, or starting with `/`) logs no `stages` and so no flag. `MUNINN_NO_CALLLOG=1` stops the log. These are facts about a call,
not proof that the answer was missing; only the labeled replay in
`docs/REPLAY-BENCHMARK.md` shows true misses.

## Current knowledge and learning

Knowledge IDs are `K_` plus 32 lowercase hexadecimal characters; request IDs use
`R_` with the same shape. Numeric knowledge aliases are rejected. List/search
return `knowledge: [KnowledgeView, ...]`; a single typed view keeps its fields at
this JSON object's root alongside the common notice/freshness/logging metadata.

| Command | Meaning/output |
|---|---|
| `muninn know list [--all-projects] [--task TASK]` | Current eligible `KnowledgeView` objects, newest first. |
| `muninn know search QUERY [--all-projects] [--task TASK]` | SQLite FTS5 search of current permitted knowledge; no embeddings. |
| `muninn know show K_ID [--revision N]` | `KnowledgeView` fields plus `evidence_sources` mapping each `E_` ID to its exact transcript `ref` and `line_sha256` for that revision. Unavailable sources have null locators. Historical revisions require `--revision`. Explicit show may retain withdrawn content. |
| `muninn know context [--task TASK]` | `KnowledgeContext`: `schema_version`, complete note views in `notes`, compact `guides` (`id`, `revision`, `title`, `scope`), `unresolved_ids`, `truncated`. Bound by installed policy's `context_bytes`. |
| `muninn know propose --input FILE` | Strict `ProposalInput`, then a checked current local attempt and `Receipt`; no model invocation. |
| `muninn know retract K_ID --expected-revision N --reason TEXT` | Checked withdrawal of that current revision; returns `KnowledgeView`. |
| `muninn learn request --session REF [--from-line N --through-line N] [--policy PATH]` | `RequestView` for a bounded request; paired bounds or activation/uncompleted boundary through the latest complete record. |
| `muninn learn list [--state STATE]` | `requests: [RequestView, ...]`; state is `pending`, `running`, `waiting`, `completed`, `failed` or `cancelled`. |
| `muninn learn show R_ID` | `ReviewDetail`: request, attempts, durable calls, governed evidence and receipt/null. Content is shown only when requested, never logged. |
| `muninn learn cancel R_ID` | Current `RequestView` after cancellation. |
| `muninn learn retry R_ID` | Explicit restart of the same request, including cancelled requests when current guards permit. Completed requests retain their receipt/state without another commit. |
| `muninn learn resume --policy PATH --failed-request R_ID` | Re-enable stopped ongoing Codex admission after inspecting the latest exact `rejected_authority` failure. The file must equal the installed policy. All attempts must be terminal, all calls completed, and no other failure type, abandoned attempt or pending work may exist. Failed requests remain failed; output is `{"admission":true,"policy_id":...}`. |
| `muninn learn replay --session REF --from-line N --through-line N --policy PATH` | Explicit bounded historical request through the same evidence/commit path; no automatic archive review. |

`REF` for learning is an indexed `provider:thread_id` or an unambiguous session
root. Request creation is admission, not completed review. Missing or invalid
installed frozen policy refuses with `review_policy_required`; an explicit policy
must match the admitted installed policy, not supply guessed model settings.
Policy records explicit runner/model/reasoning, bytes/rounds, deadlines,
retries/backoffs, concurrency and budget. This CLI does not install a policy.

`ProposalInput` contains only `request_id` and `result`; `result` contains
`schema_version: 1`, `changes`, `unresolved` and `no_change_reason`. Use evidence IDs
from this request's admitted detail; IDs, claims and local files confer no owner
authority. Proposal claims/fences the request's current attempt without stealing
supervisor ownership, then rechecks physical evidence/revision/generation before
atomic commit. Claim and commit use separate writer roots: intervening erasure or
cancellation can refuse the commit. A successful receipt records `request_id`,
`attempt_id`, `applied` revision pairs, `unresolved_ids`, `committed_at`, `outcome`
and nullable `no_change_reason`. Existing successful receipts remain idempotent.
Human correction uses a supported proposed replacement with `knowledge_id` and
`expected_revision`; only explicit retract performs withdrawal without replacement.

A note change requires `action: add`, `form: note`, `kind`, `scope`
(`project_id`, `task`), `content`, `conditions` (a list), nonempty
`evidence_refs` and `reason`. A guide change uses `form: guide`, `title`,
`summary`, `applicability`, `steps`, `decision_points`, `pitfalls` plus the same
kind/scope/reason. Each supported claim has `text` and `evidence_refs`; each guide
step has `key`, `claim` and `depends_on` referring only to preceding steps.
A replacement uses `action: replace` and additionally requires `knowledge_id`
and `expected_revision`. `unresolved` entries have `claim`, `affected`
(knowledge/revision pairs), `evidence_refs` and a specific missing-evidence
`question`. `no_change_reason` is nonempty only when changes/unresolved are empty;
otherwise it is null. Strict validation rejects extra fields and coercion.

`know add`, `know check`, old tags/confidence/contradiction/loop flags and numeric
ID coercion are retired. They have no compatibility aliases. `quote-check` still
checks verbatim event text, but a match alone does not grant proposal authority.

Current retrieval excludes disputed/withheld, expired, superseded, retracted,
erased or unsupported guidance even if stale FTS rows remain. Project guidance
applies to that project and its tasks; task guidance only to the same task.
Global guidance is not inherited by project hooks. `--all-projects` explicitly
unions read scopes and preserves each result's actual scope; it does not grant
cross-project authority. Notes retain complete conditions and support/expiry;
guides in context are navigation only. Open the requested current guide revision
and cited originals before relying on its guidance. Already injected context
cannot be retracted; request a fresh pull after changes.

The current store requires both schema version 1 and the
`store_meta` marker `muninn-knowledge`. Historical unmarked v1/v2/v3 stores need
an explicit cutover; ordinary writers and rebuild refuse them. A numeric pragma
alone is not a format or maintenance-authority certificate. Legacy activation and
restart are not implemented by the current-format maintenance route.

A current-format installer upgrade retains the live pathname. After proven
quiescence it fences review attempts, preserves unknown charges and validates the
permitted policy before activation. One actual maintenance owner spans pin/config;
the writer is released during those steps and the owner before service start.
Once live mutation may have happened, failure keeps current data and the selected
runtime/configuration, leaves the service stopped, and records
`outcome: recovery_required`. It does not restore an archive over newer data.
Existing governed archives are retained. Any installer path that actually replaces
a live store still requires its durable conservative publication record before
replacement and before runtime/configuration rollback decisions.

### Exact legacy exclusion and forward cutover

For the exact inventoried legacy release `5c1a69b0edff81df80836d72a80971a91a8f9520`
on macOS/Linux, the installer can stage managed launch exclusion. This requires
stopping, and not starting, old manual commands, source/module invocations,
imported clients and custom jobs throughout upgrade and recovery. It preserves the
old database, tombstone key and erasure journal; prepares the candidate runtime;
replaces the stable launcher with a terminal maintenance response; retains the old
release in the private rollback directory; and removes the persistent service
registration. The old `current` link deliberately remains selected but dangling.
One stable private installer lock serializes upgrades and direct recovery.

Only after those exclusions are durable and revalidated does the installer record
`outcome: restart_required` and a supported boot identity. Ordinary commands remain
unavailable; hooks return compact `{"error":"maintenance","reason":"restart_required"}`
with exit 0, without queue acknowledgement. The installer exits nonzero because the
upgrade is incomplete. The same `--upgrade --repo ... --sha ...` discovers the
pending record before ordinary preflight. Same-boot resume refuses. A later boot
must still match the original source, selected interpreter, candidate, private
record and every exclusion. Missing or changed evidence yields refusal or
`recovery_required`; replacing a lost fence cannot reuse its old boot baseline.

The public installer continues after verified later-boot closure. Under the
retained operation lock and one maintenance owner, it validates the exact schema3
source and strict erasure intent, retains a durable private
`muninn.sqlite.pre-upgrade-*` archive, and copies the seven base tables into an
unpublished marked1 target. Indexed Cursor and missing-original history survive;
legacy knowledge remains in the archive. Target replay completes pending erasure
before replacement, without changing the legacy source, external key or journal.
Archive and incomplete copy artifacts remain visible as erasure residue.

Before one replacement attempt, the private record persists
`publication: possible` with source, candidate, archive, home, lock, intent and
prepared runtime bindings. A replacement error can mean the new store is already
live. Recovery reads actual identity and format, retains every archive, and never
restores old data/runtime or promotes an orphan. An unchanged original can be
retried by building a fresh candidate. A proven marked replacement advances using
the current witness rules; missing/uncertain witness after compatible runtime
exposure cannot be repaired with an old installer boot observation.

Compatible configuration completes while the maintenance launcher remains in
place. The final writer revalidates the live path and replays new erasure intent.
First policy activation, if requested, completes before removing that launcher;
all maintenance roots close before service start. Interrupted unrecorded provider
configuration remains excluded with `recovery_required`, because replaying a
plugin mutation could overwrite its original before-values. Successful legacy
operations retain their private recovery record and archive metadata. The
preparation-only `verify_legacy_resume` interface still stops at
`legacy_cutover_pending`; it does not perform publication.

### Installer first-policy modifier

`--review-policy PATH` is an optional modifier to the full existing installer
invocation: `bin/muninn-install --repo DIR --sha SHA --fresh|--upgrade
--review-policy PATH`. It is currently supported for macOS/Linux. The wrapper
continues forwarding advanced arguments unchanged. Input is bounded UTF8 strict
JSON: duplicate keys, nonfinite values, linked input, malformed policies and
changed frozen snapshots refuse before disruption. The private record contains
raw bytes/digest and canonical policy/digest, bound to the operation, home and
prepared release. Install summaries contain `policy_status`, never policy values.

Absent input yields `activation_pending` for a never-activated store. Existing
policy and source baselines are immutable: identical input is a no-op preserving
admission, including zero; different input requires a separately authorized
policy transition. First activation records fixed time, store identity,
generation and sorted source-boundary digest before committing policy and
admission together. Recovery recognizes an exact committed activation without
resetting those values; an already fenced admission remains zero and reports
`activation_incomplete`. Records survive uncertain commit/completion failures.

A valid policy grants no native capability, human production-policy approval or
budget authority. Prompt, capability, host and admission checks still apply.
An explicit `experimental_codex` policy may select
[ongoing Mac Codex production](learning/ongoing-codex-production.md) with a
separate `ongoing_production` budget kind. It has no aggregate quota or expiry;
per-call controls, ordinary project scopes and durable failure halts remain.
The finite pilot account remains separate. This does not grant verified runner
status or full C6 acceptance.
Windows legacy boot/termination proof and actual native/live qualification remain
separate requirements. Preserve the private operation record, source/archive,
key and journal during recovery; manual restoration of the old launcher or
service is unsupported.

## Running and maintaining

| Command | What it does |
|---|---|
| `muninn ingest [--full]` | catch up with Claude and Codex provider transcripts now; `--full` rescans everything and imports Cursor history from `MUNINN_CURSOR_DB` when supplied, or `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` on macOS. Windows and Linux require an explicit database path; no layout is guessed. Cursor data is read-only and is not polled. Answer `ingest`: `files_seen`, `files_changed`, `events_added`, `events_removed`, `skipped_files`, `unreadable_files`, `skipped_lines`, `failed`, `errors`, `missing`, `duration_s` |
| `muninn serve [--interval S]` | the managed poller loop (default 60 s); prints nothing |
| `muninn stats [--usage]` | counts, see below |
| `muninn doctor` | health checks, see below; exit 1 if any error-level check is `false` |
| `muninn compact` | VACUUM the database under the writer lock; answer `compact.bytes_before`/`bytes_after`, `db_space`; refuses without about one database of free disk |
| `muninn rebuild` | rebuild only a verified marked current store. Copy every governed family, retain DB-only Cursor and missing-source history, then fully re-ingest transcripts and replay current erasure intent. Fence admission/attempts and retain a private `muninn.sqlite.recovery-<id>` archive before candidate work. Unsupported, unreadable, changed or incomplete sources refuse; publication never restores an archive automatically. See the recovery contract below. |
| `muninn erase --session S \| --event REF \| --match TEXT [--dry-run] [--yes]` | forget selected events and associated revisioned knowledge, evidence, requests and unresolved material; without `--yes` it is a dry run. Real erasure coordinates whole-service quiescence before writing durable tombstones and scrubbing content. Reports cleanup limits and nested `erasure_status`; see the erasure section below. |
| `muninn hook session-start\|prompt --provider claude\|codex\|cursor` | session and prompt hooks: payload on stdin, JSON on stdout (see the README) |
| `muninn hook session-end\|pre-compact --provider claude\|codex\|cursor` | Capture bounded learning windows; Cursor imports the exact conversation before capture, with preCompact bounded by `message_count` and sessionEnd independent of that field. All hooks return compact provider-compatible JSON and exit 0. |


Rebuild retains one maintenance owner and one acquisition deadline. Full transcript
passes use fresh connections inside separate writer regions; fixed nonmaterial Git
preparation runs between them with the writer free and all material connections
closed. The original complete source/key/intent fingerprint is checked before each
re-entry. A concurrent writer, changed file identity or unsettled sidecar refuses as
`source_changed`. Only the database ctime change caused by the final writer's own
private-permission normalization is tolerated within that same writer region.
Own replay that changes source bytes also refuses. No failed or unreadable ingest
attempt is hidden by a later successful pass.

On success `rebuilt` and `old_readable` are true; `old_kept_as` names the retained
archive, `copied` holds verified table-copy counts, `reapplied_tombstones` counts
new intent rows applied to the candidate, and `ingest` holds invocation work totals
as defined below. `warning` explains archive retention. `recovery_required` is
false only for completed publication or refusal before uncertain mutation.
`publication_possible` becomes true before attempting replacement and remains true
on success or failure; an existing orphan candidate also keeps it true because
prior exposure is uncertain. A failed replacement returns exit 4 `replace_failed`
without an `old_restored` claim. Refusals return exit 2 with a bounded `reason`;
writer contention remains exit 3. Check `recovery_required` even for a busy result.

After any failure, preserve the actual live file, `tombstone.key`, current
`tombstones.jsonl`, candidate and every archive as they exist. No restore, orphan
promotion, retry or inferred admission reenable occurs. A crash before publication
may leave admission off; a later invocation must not guess its lost prior value.
A missing answer or log does not prove nonpublication. A fresh invocation validates
current state again and refuses an existing `muninn.sqlite.rebuild` or unsafe live
state. A retained archive, including a partial copy, is recovery evidence rather
than permission to overwrite current data. Archive removal requires a separate
owner decision; erasure reports retained archives as residue.


Ingest counters describe work across the completed invocation. `files_seen`,
`skipped_files` and `unreadable_files` count visits or open attempts across passes,
not distinct files. `files_changed`, `events_added`, `events_removed` and
`skipped_lines` sum committed work. A `--full` retry remains full and counts its
repeated committed replacements. `failed` and `errors` include ordinary failed
read attempts, even if a later attempt recovers that source; Git preparation
misses do not count as failures. `missing` comes from the completed terminal pass,
and `duration_s` includes preparation and lock waits. A completed sweep can still
contain failures.

Each pass holds the writer separately. Missing Git membership facts are probed
while unlocked, then a fresh connection replans provider files and database state.
There is no total retry or runtime limit under continuously changing inputs; each
Git probe retains its ten-second timeout. Git facts reflect when they were
observed, not a coherent or current repository snapshot. The poller publishes its
completed heartbeat and clears its prior error only after the terminal pass;
intermediate preparation can update liveness without claiming completion.

## Installing and removing (from a checkout)

| Platform | Installed desktop app evidence |
|---|---|
| macOS | `Claude.app`, `Codex.app`, `Cursor.app` in `/Applications` or `~/Applications` |
| Windows | Desktop executables in LocalAppData/Program Files; current-user `Claude`/`OpenAI.Codex` MSIX registration with an existing executable declared in its manifest |
| Linux | `claude-desktop`, `chatgpt`, `cursor` on PATH |

- Absent app: setup/config validation/installed-hook verification skipped.
- Installed Claude/Codex without config: manual setup guidance.
- Leftover settings/transcripts: never installation evidence.
- History import/existing index: independent of app detection.

Fresh installation finishes transcript indexing and available Cursor history
before activating the release. Its checklist reports the actual stored event
counts by provider. Incomplete ingestion or erasure-cleanup failure prevents
activation; already committed erasure remains in effect. Recall starts off, and
learning admission remains off until explicitly configured. Service-start
verification requires a new heartbeat after the completed initial ingest.

| Command | What it does |
|---|---|
| `bin/muninn-install` | fresh install, or upgrade when installed (the last step moves each old release to `.pruning-<ts>-<sha>` so a failure can still roll back, then a sweep deletes those dirs after the point of no return; a sweep that fails warns, is listed as `sweep_failed` in the install record, and the next upgrade retries; `muninn doctor` warns with `release_leftovers` while a `.pruning-<ts>-<sha>` directory remains in `~/.local/lib/muninn`, which is where they live, not in the data directory); `--check` previews, `--status` compares the installed commit with `HEAD` |
| `bin/muninn-uninstall [--dry-run] [--purge-data]` | the same as `bin/muninn-install --uninstall`. Stops the poller; removes the launchd plist, Muninn's hooks from Claude's `settings.json` and Cursor's `hooks.json`, Muninn's sections from Codex's `config.toml`, the Codex plugin cache, `~/.local/lib/muninn` and the `~/.local/bin/muninn` link (only when it points into that release directory). Moves the data directory to `~/.local/share/muninn-removed-<ts>/muninn` (`--purge-data` deletes it instead). `--dry-run` writes nothing. Prints one line per action, then `uninstall done`, `dry run only: nothing was removed` or `muninn is not installed here`, and exits 0. A provider config it cannot edit safely, a taken data-move name, or a job launchd will not unload prints `FAILED: <why>` and exits 1; the config and name checks run first, in a dry run too, before anything is changed |

## `muninn erase`

Choose exactly one session, event reference or literal match (at least four
characters after trimming surrounding whitespace). Event-reference syntax is
checked before coordination;
missing or ambiguous event resolution happens inside the governed store. Unknown
sessions retain their existing zero-target/tombstone behavior. `--match` searches
event text and governed knowledge content, reasons, expiry conditions, claims,
evidence excerpts and unresolved content. Erasure follows support and review-read
associations through prior revisions, withdraws whole affected guides, clears
explanations and fences affected requests. IDs, statuses and uncertain charge
history can remain; removed text is not written to the call log.

Real erasure holds the owner barrier before the writer barrier. It may stop the
**whole Muninn serve process**, including unrelated ingestion, and waits for
contained children, discarded process buffers and durable completion before
changing requested content. It selects at most one owner; a replacement produces
`owner_changed` rather than another stop request. A missing store now returns exit
4, `store_unavailable` with detail `store is absent`, without creating a database.
This differs from the older erase behavior. Dry runs retain their writer-only,
no-stop path and may initialize an empty store; they create no `tombstone.key`.
Neither an absent nor an empty store establishes process-containment evidence.

The Python APIs retain `run_erase` and caller-owned `erase`; the latter requires
established quiescence as well as writer ownership for real erasure. Callers needing
observable status use `run_erase_request(home, EraseRequest(...), status=...)`.
The existing `wait_s=15.0` default is unchanged. For real erasure it is one
coordination-acquisition budget, including owner and writer waits, **not** a limit
on repair, physical I/O or total command duration. `wait_s=0` attempts immediate
acquisition without publishing a stop, recording a repair baseline or sleeping;
writer contention still raises Busy. There is no new CLI timeout flag.

Every handled erase result, including mapped failures, adds `erasure_status`:

| Field | Meaning |
|---|---|
| `stop_may_have_been_published` | Conservative latch set before possible stop visibility; it does not say whether the service is currently running or stopped. |
| `repair_phase` | `none`, `baseline_recorded`, `reconciling` or `complete`, independently of the requested erase. |
| `erasure_phase` | `not_started`; `intent_may_be_durable` before the append; `intent_durable` after its durable return; `committed` after SQL commit; `verified` only after existing checks complete with zero vocabulary remainder, zero reported residue and zero retained archives. |

Only when a stop may have been published, status also contains
`restart: "not_attempted"` and `next_action: "check_and_restart_if_needed"`.
Check the service and arrange a manual restart if needed, including after a failed
erase. Muninn never automatically restarts the service or reboots the host, and
this status never asserts that service availability was restored. If containment
is uncertain, a supported boot observation can record a bound repair baseline;
the same boot refuses with `reboot_required`. A later supported boot permits
reconciliation only while all required bindings still match. Do not delete witness
or repair-baseline files to bypass refusal. Windows boot repair and retirement of
an existing repair baseline remain **unsupported required-platform release
blockers**. Normal valid-complete ownership without a baseline is a separate path;
refusal is not completed Windows recovery support. Native containment and boot
qualification remain separate release checks.

Write-ahead tombstones survive interrupted erasure and are replayed before ordinary
quiescent startup/erase entry. Pending intent refuses new foreground proposals and
commits; an existing receipt still has receipt-first audit access until replay
scrubs its governed explanation. The journal guard reads the whole log, with no
constant byte, memory or duration bound. Preserve the current journal and durable
`tombstone.key`; losing the key prevents keyed tombstones from being checked.
Event content tombstones are keyed HMACs of role/text and apply only to fork replay,
not to a later identical turn. Existing family copies are erased conservatively:
all matching ancestor events, and only the replayed leading run of descendants or
siblings. A historical erasure lacking a content tag can still suppress a byte-for-
byte line copy; a differing copy is not recognized by that line tombstone.

The `review` counts include removed working files and `retained_archives`.
Only program-owned `review-work/<AttemptId>` material is removed after containment;
symlink targets, unrelated siblings and provider transcripts are preserved.
`vocab` and `residue` report the existing bounded-needle checks. `verified` is
**not universal forensic absence**, nor a guarantee about remote/provider copies.
Retained archives keep the phase `committed` even when no selected needle matches.
Commands that finish with residue still return exit 0. `aside_files` lists retained
unreadable, recovery and pre-upgrade stores; `aside_remove` provides the literal
manual removal command (`rm --` on POSIX, `Remove-Item -LiteralPath` on Windows).
These archives are never deleted automatically. Report `provider_files`,
`not_covered` and `out_of_scope`, including backups and free filesystem blocks.

Existing mapped exits remain: Busy 3; hot journal, tombstone key, unavailable store
or filesystem access 4; value/lookup refusals 2. Status preserves the latest reached
milestone and safe existing error details. Arbitrary SQLite failures still
propagate; no output is guaranteed after process death or output failure.

## `muninn stats`

`sources` counts per `provider/root/thread_class/status` (status `active` or `missing`);
`events` per kind; `events_by_provider`; `flags` `marker` (text that looked like an
injected block), `redacted` (secret removed), `truncated` (over 64 KiB); `skipped_lines`
and `issues` (lines not parsed: `line_too_large`, `invalid_json`, `too_deep`,
`not_object`); `other_threads` unrecognised threads by reason; `knowledge` by durable
state (`active`, `withheld`, `retracted`, `erased`); `evidence` by state (`live`,
`changed`, `erased`); `tombstones` by level
(`session`, `thread`, `line`); `db_bytes` file size; `db_space` `page_count`,
`freelist_count`, `page_size`, `free_ratio`; `last_pass` (`last_pass_at`, `duration_s`,
`files_changed`, `events_added`, `skipped_files`, `unreadable_files`, `last_error`
(class name of the last failed pass, null after the poller's next good pass), `failed`,
`errors`, `busy_skips`, `index_age_s`, `poller`, and `alive_age_s`, the seconds since a
running pass last stamped alive: null between passes and when no usable stamp exists,
and growing if the poller was killed mid-pass and not restarted); `reread` (`pending`
active sources still read under an older classifier version, `of` all active sources:
the progress of the one-time re-read after a classifier change; it normally falls to 0,
and a source that remains failed or skipped stays pending. Inspect
`last_pass.failed`, `unreadable_files` and `skipped_files` as invocation attempt
totals, which can include later recovered sources, not unresolved source counts); `install_sha`; `classifier_version`;
`hash_mismatches` (open-time line hash failures seen in the call log). `--usage` adds
`usage` (per session `calls`, `errors`, `last_ts` of muninn calls seen in transcripts)
and `usage_totals` per provider. `stats` always reports `retrieval_outcomes` (counts from
the same call-log window: `window_lines`; `search` `calls`, `zero`, `outside`, `noterms`;
`hook_prompt` `calls`, `zero`, `outside`, `floor`; calls without `stages` count in `calls`
only; facts, not proof of a miss).


`review` is a schema-1 `ReviewStats` object. `requests_by_state` and
`calls_by_state` count durable rows; absent states have zero rows.
`oldest_pending_age_seconds` covers pending/waiting requests;
`last_progress_age_seconds` measures the newest durable attempt progress. Ages are
nonnegative, or null when there is no matching row. `unresolved_items` counts open
items. `usage` sums actual per-call reports once: unknown components stay null,
unconcluded calls stay unknown even with retained usage, reservations never become
measured costs, and mixed/unknown or unsafe currencies leave cost
and currency null. Diagnostic currency must match `[A-Z]{3}`. `--usage` remains
separate transcript-observed CLI usage.

`eligible_records` and `completed_records` count currently indexed logical
prompt/reply records, deduplicated by provider, thread and physical line. They are
neither absolute source cursors nor lifetime counts of erased/removed data.
Activation archive records are excluded unless explicitly admitted for historical
review. Overlapping admitted windows count once; only success receipts, including
no-change receipts, can count completion, and only with current indexed provenance.
The request endpoint must still match its admitted hash; that hash proves only the
endpoint. Interior records require matching live evidence linked to the same
request. Missing/changed/erased input or unprovable provenance remains an uncertain
gap, reducing metric availability without changing historical audit receipts.
No original transcript is read by health, so these are indexed observations rather
than live physical validation. A later explicit receipt can count work while
an earlier gap remains. Historical replay never changes the activation baseline.
Durable states, receipts and gaps survive log rotation or disabled logging; exact
per-source reconciliation refusal reasons are not durable.

`observed_enqueue_busy` counts only retained `review_enqueue_busy` log records (null
when no readable valid log record remains). `busy_count_complete` is always false.
Zero observed busy records never proves no missed acknowledgment. Later recovery
can capture indexed content, but cannot prove the exact lost lifecycle event or
cutoff. Cursor recovery waits for the next native import or explicit full ingest.
Governed explanations belong in explicitly requested `learn show`, never health or
logs. An `active` knowledge state alone does not establish current eligibility.

## `muninn doctor`

Answer: `ok` (true when no error-level check is false) and `checks[]`. Each check has
four fields:

- **`check`**: the check's name.
- **`ok`**: `true` passed; `false` failed; `null` could not be verified.
- **`level`**: how much a failure matters. `error`: `ok: false` makes the whole answer
  `ok: false` and the exit code 1. `warn`: shown, does not fail the run. `info`:
  informational; `null` still means unverified.
- **`detail`**: a short string whose meaning depends on the check (below); empty when
  there is nothing to add.

| check | level | passes when | `detail` |
|---|---|---|---|
| `data_dir_mode` | error | the data dir is mode 0700 | its mode, or `absent` |
| `file_modes` | error | no data file is readable by group or others | names of loose files |
| `unexpected_files` | error | only known files are in the data dir | names of stray files |
| `unowned_journal` | error | no crashed writer's journal is lying around | explanation |
| `store_readable` | error | the database opens read-only and has the right schema | the refusal message if not, such as `store format marker missing` or `cannot read the store (SQLITE_BUSY)` |
| `journal_mode` | error | SQLite journal mode is `delete` | the mode |
| `fts_secure_delete` | error | both full-text indexes have secure delete on | none |
| `quick_check` | error | SQLite's `quick_check` says `ok` | its first 80 characters |
| `missing_sources` | info | always | count of indexed files no longer on disk |
| `other_threads` | info | always | count of unrecognised thread types |
| `reread` | info | always | `N of M`: active sources still to be re-read after a classifier change; inspect `failed_sources` and `stats` `last_pass.unreadable_files` as attempt totals, not distinct or unresolved source counts |
| `db_size` | warn | the database is under 2 GB | its size; the threshold |
| `db_free_space` | warn | free pages are under 25% or under 64 MB | free size and ratio; `run: muninn compact` |
| `writer_secure_delete` | error | writer connections turn secure delete on | none |
| `heartbeat` | error | the poller finished a pass, or a running pass stamped `alive_at`, within 3 intervals (`poller: ok`) | `index_age_s` |
| `failed_sources` | warn | no ordinary source read attempt failed during the last completed invocation | failed read attempts, including repeated attempts and possibly subsequently recovered sources; preparation misses are excluded. The SessionStart block, and a recall block that has a hit, also report these attempts |
| `poller_error` | warn | the last poller pass did not end in an exception; the poller's next good pass clears it, a manual `ingest` does not | the exception class name only |
| `unreadable_files` | warn | no transcript open attempt failed during the last completed invocation | failed open attempts (permissions; a vanished file does not count), possibly including subsequently recovered sources. The SessionStart block, and a recall block that has a hit, also report these attempts |
| `aside_files` | warn | no historical unreadable stores or retained recovery copies remain | names of retained store artifacts; inspect the actual reported paths |
| `upgrade_snapshot` | warn | no pre-upgrade copy of the store is left in the data dir | names of leftover `muninn.sqlite.pre-upgrade-*` files |
| `tombstone_key` | error | `tombstone.key` is whole (32 bytes), and present whenever keyed tombstones exist | `missing` or `damaged` |
| `release_leftovers` | warn | no `.pruning-*` release directory is left in `~/.local/lib/muninn` | their names |
| `launchd_job` | error | on macOS, the launchd job is loaded with a live process | its pid |
| `managed_service` | error | on Linux/Windows, the managed user service/task is owned and its heartbeat identifies the actual writer; unavailable inspection reports `null`, never success; paths compare by resolved identity; each command is limited to 60 s and a timeout fails the check | constant code and process id |
| `roots_readable` | error | every provider root that exists is readable | names of blocked roots |
| `roots_present` | info | always | names of provider roots that do not exist |


Review diagnostics also use the same four-field check contract:

| check | level | meaning of `detail` |
|---|---|---|
| `review_backlog` | info | durable pending/waiting request count |
| `review_policy` | warn | installed durable policy configured, or absent/invalid while admission is enabled |
| `review_auth`, `review_config` | warn | attempt-history counts with those finite error codes; invalid stored attempt policies count as configuration failures |
| `review_process_deadline` | warn | recorded deadline failures, or unfinished attempts past their explicit policy deadline with unended calls; not proof of a live process or completed kill |
| `review_commit_delay` | warn | overdue running attempts without an unended call, awaiting durable finalization |
| `review_cancel_delay` | warn | overdue cancelled requests with unfinished attempts |
| `review_abandoned` | warn | recorded abandoned attempts, or unfinished attempts from an older generation |
| `review_coverage_gaps` | warn | the same eligible set as stats, including admitted historical replay, lacking success receipts with current matching provenance; untouched activation archive stays excluded |
| `review_capture_gaps` | warn | indexed new records beyond the eligible cursor or eligible records whose prior admission provenance cannot be verified, even after refusal or log loss |
| `review_quiescence` | info | witness presence only; always null because containment is unverified |
| `review_repair` | warn | repair-baseline presence requires manual recovery assessment |
| `review_erasure_intent` | info | journal presence, with consistency unverified; presence does not prove an outstanding intent |
| `review_windows_repair` | warn | Windows boot repair and durable repair-baseline retirement remain unqualified |

Doctor never initializes, resets or deletes supervisor/repair records. Missing
DB, absent PID or free locks do not prove containment. Preserve
`review.supervisor.lock`, `review.supervisor.json`, `review.supervisor.repair.json`,
`tombstones.jsonl`, keys and retained archives. Do not delete protocol evidence to
bypass a refusal. Erase may interrupt the whole service; inspect its existing
`erasure_status` and `restart: not_attempted`, then check and restart manually if
needed. A retained archive prevents verified erasure even if selected needles are
absent. Windows uncertain-state repair and baseline retirement are required-platform
release blockers; passing fakes or ordinary absent-baseline operation are not native
qualification. Original-source read budgets bound neither erasure-journal work nor
total I/O or command time. Process deadlines do not bound delayed DB commit/cancel.

## Per-prompt retrieval modes

### Reviewing automatic recall

Each nonempty recall block asks the model to tell you
`Memory R<number>: useful/noise/uncertain — reason`. This is a model assessment,
not a correctness guarantee. The hook still searches both prompts and replies;
its session-start guidance tells agents to use `search --kind prompt` for owner
requests and `--kind reply` for assistant reports, widening when necessary.

`muninn recall-review [--batch N] [--ack] [--pretty]` reads a batch of at most
50 deliveries. The default is the oldest unreviewed batch, or the current partial
batch (the latest batch if everything is acknowledged). On delivery 50 and later
nonempty blocks, a `systemMessage` gives the human a prompt to paste into Claude
or Codex to run the review command and follow `review_prompt`. The model
context retains a review reminder until
`muninn recall-review --batch N --ack` acknowledges that next complete batch.
Counts combine Claude and Codex; ratings are split by provider.
No timer or background model runs. The reminder appears when a hook runs.

The separate private `recall-review.sqlite` holds only ids, timestamps, source
references, excerpt caps, request/session fingerprints and context hashes; never
conversation text. It survives restarts, ordinary log rotation, upgrades and
index rebuilds. Source refs remain authoritative when event ids change. It counts
generated context, not proof that the host accepted it or the model read it.
`MUNINN_NO_CALLLOG=1` disables the ordinary call log, not this review metadata.
If metadata can't be written, recall stays available with a review-unavailable
instruction; the call log carries `review_error: 1` and that delivery is uncounted.

The report includes `total`, `reviewed_batches`, `batch`, `complete`, `deliveries`,
`review_prompt`, `acknowledged`, and (for nonempty batches) `self_assessments`.
Each delivery holds its `id`, `at`, `provider`, `session`, `prompt_sha256`,
`scope_ids`, final `sources` (ref and excerpt cap), final `knowledge` (`id`, `revision`, `evidence_refs`,
`cap`, `sha256` of the source preview, and `evidence_sources`), `context_sha256`, and `characters`. Once indexed, a
unique request matching its redacted text hash, provider/session and timestamp
within two minutes supplies `request_ref` and `request_preview`; otherwise
`request_status` is `unresolved`. `assessment` contains the model's matching
`Memory R<number>` rating before the next prompt, its source ref and reason preview,
or `unassessed`. Previews require opening originals before factual claims.
Both request and assessment previews are redacted again before printing, including
text indexed before a redaction rule existed. A store that becomes unavailable
during request or assessment resolution returns a JSON error with exit code 4.
Historical full block text isn't retained, and current knowledge may differ.
Use `muninn know show K_ID --revision N` with the recorded UUID and revision
to inspect that knowledge version. Its E_UUIDs are evidence identities, not
arguments to `muninn open`. Each recorded `evidence_sources` row maps an
`evidence_id` to an openable `ref` and the original `line_sha256`, bound to that
exact knowledge revision. Open that recorded `ref` with `muninn open REF --context 3`;
require `hash_ok: true` and `provenance.line_sha256` equal to the recorded hash.
A recorded locator does not prove the source remains available or unchanged.
Missing mapping fields or null `ref`/`line_sha256`, missing originals and changed
hashes remain unresolved; never substitute current knowledge or other evidence.
The preview `sha256` identifies the pre-rendering source preview, not the complete
knowledge body or redacted, shortened historical block. The mapping records no
transcript text or filesystem path.
`--ack` requires `--batch`, a completed batch, and sequential review. Invalid
arguments exit 2; unavailable metadata or transcript store exits 4. Acknowledgement
records review, not approval of recall quality. Follow `review_prompt` and report
missing evidence before acknowledging.

The ordinary call log now distinguishes selected `returned_ids`/`knowledge_ids`
from `delivered_ids`/`delivered_knowledge_ids` that survive shortening.
Knowledge IDs use `K_` UUIDs; event IDs remain numeric. A recorded
block also has its numeric `delivery_id` and, when due, `review_due_batch`. No query text or fingerprints enter the
prompt-hook call log; request fingerprints live only in the review database.

### Switch behavior

**Function.** Before each prompt, `muninn hook prompt` can add current eligible
knowledge previews followed by at most three prior prompt/reply events from the
mapped project, excluding the caller's own session. It requires at least three
distinct query terms and skips slash commands. UTF-8 caps are 1,500 bytes for Claude
and 900 for Codex. With `recall.off` present, the prompt hook prints `{}`.
With `recall.advisory` present and `recall.off` absent, ordinary prompts
receive the static `<muninn-retrieval>` instruction without opening the store
or delivering automatic recall. Empty prompts and slash commands print `{}`.
With neither file present, automatic recall is on.
A nonempty automatic recall block tells the agent to inspect `K_` records with
`muninn know show`, match an `E_` ID in `evidence_sources`, and open its exact
`ref`; the IDs themselves are not `muninn open` refs. Verify the opened
original's hash against the source's `line_sha256`.
`MUNINN_HOOK_DISABLE=1` silences all hooks.

The static instruction asks the model to search on demand when project history
matters, start with `--kind prompt` for owner decisions or `--kind reply` for
assistant reports, refine or widen as needed, and open original refs before
using past claims. `K_` IDs go through `muninn know show`; `E_` IDs are not open
refs. Missing relevant evidence must be reported as unknown. This instruction
does not search or count as an automatic recall delivery.

Verified SessionStart refreshes (`startup|resume|clear|compact` for Claude/Codex)
render complete applicable supported notes and a compact guide index as untrusted
data, bounded by installed `context_bytes` and 4,000 bytes (Codex 2,800). No arbitrary
eight-item limit or actor/quote heuristic remains. Unfitting items are omitted
whole with a truthful notice. The usage text tells agents to verify current
revisions and open cited originals; preview eligibility is not a truth guarantee.
Claude/Codex `systemMessage` lists exactly displayed items with control characters
replaced and distinguishes omitted knowledge from no eligible knowledge. The
status's content is never logged. Prompt recall adds a human-facing line only when a 50-delivery review is due.

End/precompact capture admission is separate from startup refresh. Only a durable
request is internally acknowledged; SQLite busy yields the provider-safe empty
response and content-free `review_enqueue_busy`. Later recovery can capture later
indexed content, not prove the exact missed event/cutoff. Cursor waits for the next
native import or explicit full ingest. A recovery gap is not proof of coverage.
Cursor startup uses its native `sessionStart` envelope (`additional_context`);
resume/postcompact injection remains unproven. Explicitly run `muninn know context`
after resume or compaction. Context already injected cannot be retracted.

**Default for new installs: OFF.** `--fresh` creates `recall.off` (mode 0600) and prints
how to turn recall on. `--upgrade` never changes either mode file, so an existing
machine keeps its selected mode. If both files exist, off wins.

**Rationale.** Automatic recall can introduce irrelevant material from another project.
Retrieval relevance has not been revalidated, so fresh installs disable recall until
that validation passes. See `docs/adr/0007-recall-off-by-default.md`.

| To | Run |
|---|---|
| on: automatic previews | remove both `recall.off` and `recall.advisory` |
| advisory: model chooses when to search | create `recall.advisory` with mode 0600; remove `recall.off` |
| off: no prompt context | create `recall.off` with mode 0600; remove `recall.advisory` |
| check | `ls ~/.local/share/muninn/recall.{off,advisory}`; off wins if both exist |

Mode files must be 0600, otherwise `muninn doctor` fails `file_modes`. Their
contents are ignored. The change takes effect on the next prompt; no restart is
needed. Experimental Codex learning activation still requires true off mode;
advisory mode supplies prompt context and is refused by that boundary.

## Files in the data directory

`muninn.sqlite` the database; `writer.lock` the writer lock; `status.json` the poller
heartbeat (counts only; `last_error` and `last_error_at` name the class and time of a
failed pass and are both cleared by the poller's next good pass (a manual `ingest` does
not clear them); `alive_at` is refreshed every few seconds while a pass runs, so a long
re-read after a classifier change still shows `poller` `ok` while `index_age_s` keeps
counting from the last finished pass); `calls.jsonl` and `calls.jsonl.1` one allowlisted
line per CLI call (ids and counts, no text; rotated at 1 MiB); `poller.log` and
`poller.log.1` poller events (rotated likewise; the plist sends launchd's own stdout and
stderr to `/dev/null`, and a poller whose plist still sends them to the log silences
them itself, so nothing reaches the log except allowlisted lines; launchd restarts the
poller on any exit, a clean stop included, so only `launchctl bootout` keeps it down);
`tombstones.jsonl` erase records; `tombstone.key` (exactly 32 random bytes, mode 0600,
created whole the first time an erase needs it and never by a dry run, survives
`rebuild`, never logged) the key that makes the content tombstones in `tombstones.jsonl`
HMAC-SHA256 tags (`ev2:`) instead of plain hashes, so an erased short text cannot be
confirmed by guessing. A new key is never made while keyed tombstones exist, because it
would stop them matching: if the file is missing or damaged then, `ingest`, `rebuild`
and `erase` exit 4 with `tombstone_key` and `doctor` fails `tombstone_key`; restore the
file from a backup; `muninn.sqlite.unreadable-*` historical unreadable copies and
`muninn.sqlite.recovery-*` retained rebuild archives, including interrupted partial
copies. `erase` cannot scrub these retained copies, so they may hold erased text: `erase` names every such file in
`aside_files` with the exact command in `aside_remove` (`rm -- <path>` on macOS/Linux;
PowerShell `Remove-Item -LiteralPath` with a literal path array on Windows), and
`doctor` warns (`aside_files`) while one exists. It is not deleted automatically;
salvage what you need, then run that command; `muninn.sqlite-journal` SQLite's rollback
journal, allowed by `unexpected_files` but a leftover one fails `unowned_journal`;
`recall-review.sqlite` private delivery metadata and review checkpoints, with
`recall-review.sqlite-journal` its transient SQLite journal; neither stores transcript
text. `recall.off` and `recall.advisory` select prompt retrieval mode (both must
be mode 0600). Retained `muninn.sqlite.pre-upgrade-<sha>` archives (mode 0600) are also governed
residue; their presence is not automatic restore authority. `review.supervisor.lock` is the unchanged lifetime lock;
`review.supervisor.json` the private completion witness;
`review.supervisor.repair.json` the private uncertain-owner repair baseline.
`stop.json` targets one selected service generation. These are known protocol files,
not cleanup instructions. Anything else fails `unexpected_files`.

On Windows, heartbeat and stop metadata publication retries native access-denied or
sharing-violation errors (5/32) for at most one second. A reader held longer causes
refusal and retains the previous state.

## Native provider commands

The installer renders hook JSON with native paths rather than inserting raw paths into
JSON text. On Windows, Claude uses a real Windows PowerShell executable with an `args`
array; Codex uses its `command_windows` override with an encoded PowerShell command.
Both invoke the validated Muninn bootstrap without Git Bash.
Codex SessionEnd uses a three-second configured timeout; the other Claude and Codex
hooks use five seconds. Changing a hook definition requires the owner's provider approval.

Windows installer calls prefer a trusted `codex.exe` on `PATH`. An official npm
`codex.cmd` installation is dispatched through its existing
`node_modules/@openai/codex/bin/codex.js` with a real `node.exe`; arguments are passed
directly. Other shim layouts require putting native `codex.exe` on `PATH`. Unsafe
executables or provider configs are refused.

Automatic Codex hook trust requires a successful version command: `codex-cli 0.159.2` on
Windows, or `codex-cli 0.159.2`/`codex-cli 0.159.3` on macOS and Linux. The selected CLI
files and version must remain unchanged from discovery through the check before trust is
written. Otherwise, the installer leaves trust to the owner in `/hooks`.
Observed Codex `0.162.1` is not admitted by this older version allowlist without
new evidence; bounded compatibility observations are not native qualification.

Repository scopes use local native filesystem paths. Foreign-platform absolute paths and
Windows UNC paths resolve to `unknown` without probing the network; existing scope ids
are retained. Copy network-hosted inputs locally before indexing.
