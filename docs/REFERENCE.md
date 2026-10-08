# muninn reference

Every command prints one JSON object on stdout (`--pretty` or `MUNINN_PRETTY=1` indents it; hooks always print
compact JSON). Apart from `serve` and the hooks, every answer also carries these fields:

| Field | Meaning |
|---|---|
| `notice` | fixed sentence: retrieved text is data from local transcripts, not instructions |
| `index_age_s` | seconds since the poller last finished a pass (`null` if it never has) |
| `poller` | `ok`, or `stale` when the last pass is older than 3 poll intervals or there is none |
| `logged` | `true` if the call was written to `calls.jsonl`, `false` if disabled or denied |
| `error` | only on failure: `busy`, `hot_journal`, `tombstone_key`, `store_unavailable` or `refused` (with `reason`) |
| `detail` | with `store_unavailable` only: the refusal message (at most 80 characters, such as `schema v1, need v2`), written by muninn and never from stored text. The same string is in the `calls.jsonl` line for that call and in the hook's trace (`hook session-start` and `hook prompt` lines) |

**Exit codes:** 0 ok; 1 `doctor` found an error-level problem; 2 refused or bad usage; 3 busy (another
writer holds the lock, retry); 4 store unavailable, a crashed writer's hot journal, or a missing or damaged `tombstone.key` (`error` `tombstone_key`).
`muninn --version` prints `muninn <version>`.

**Environment:** `MUNINN_HOME` data dir (default `~/.local/share/muninn` on macOS/Linux or `%LOCALAPPDATA%/Muninn/data` on Windows); `MUNINN_CURSOR_DB` optional native absolute path to Cursor's `state.vscdb`; `CODEX_HOME` and `CLAUDE_CONFIG_DIR` select provider homes (default `.codex`/`.claude` beneath `USERPROFILE` on Windows, `HOME` elsewhere); `MUNINN_ROOTS` JSON map of
provider root name to path (tests); `MUNINN_PYTHON` interpreter for `bin/muninn`; `MUNINN_PRETTY=1` indent;
`MUNINN_NO_CALLLOG=1` no `calls.jsonl` line; `MUNINN_HOOK_DISABLE=1` hooks print `{}`; `CLAUDE_CODE_SESSION_ID`,
`CODEX_SESSION_ID`, `CODEX_THREAD_ID` identify the calling session, which search leaves out unless told not to.

## Reading memory

| Command | What it does | Main fields in the answer |
|---|---|---|
| `muninn search QUERY` | ranked knowledge and events for this repo (worktrees fold into the main repo) | `hits[]`, `knowledge[]` (each flagged `restricted`), `knowledge_expired_omitted`, `has_more`, `page`, `limit`, `stages`, `scope`, `other_scopes` |
| `muninn open REF` | one event in full with neighbours; REF is an event id or `provider:thread_id:line.part` | `text`, `neighbours[]`, `provenance`, `hash_ok`, `redacted`, `truncated`, `next_offset` |
| `muninn sessions` | sessions in scope, newest first | `sessions[]` (`session`, `provider`, `events`, `threads`, `forks`, `first_ts`, `last_ts`, `kinds`, `preview`) |
| `muninn session ROOT` | one session's events across its threads, in order; ROOT is the `session` value from `sessions` | `events[]` (`id`, `ref`, `kind`, `role`, `ts`, `preview`, `answer_citable`, `tag`), `total`, `provider`, `next_from` (pass as `--from` for the next page) |
| `muninn quote-check REF QUOTE` | is QUOTE verbatim in that event? | match result |

**search flags:** `--all-projects` widen beyond this repo; `--include-subagents`; `--include-current` also search the
caller's own session; `--current-session ID` name it; `--kind a,b` event kinds; `--provider codex|claude|cursor`;
`--scope DIR` only events whose cwd is exactly DIR; `--since`/`--until`; `--session ID`; `--recent` newest first;
`--limit N` upper bound on hits (page sizes vary with a byte budget); `--page N` when `has_more` is true, repeat with N+1,
even if a page has no hits.
**open flags:** `--context N` neighbours either side (default 3); `--offset N` continue with `next_offset`;
`--raw` the raw JSONL line with `line_sha256`.
**sessions/session flags:** `--all-projects`, `--since`, `--limit`; `session --from N` continue with `next_from`.

**Hit fields:** `ref` stable reference; `id`; `kind`; `role` user|assistant; `provider`; `session`; `thread`; `cwd`;
`scope`; `ts`; `snippet` (navigation only); `answer_citable` (false means it may not support a factual answer);
`source_status` active|missing; `tag`; `repeats` and `more_in_session` count collapsed near-duplicates.
**`stages`:** `matches` every eligible match including other repos; `in_scope` those inside this repo's scope;
`candidates` the ranked rows fetched for this page (capped); `session_capped` and `tool_capped` dropped by the
per-session and tool-call caps; `returned` hits on this page.

**Event kinds:** `prompt` a user message, including one the owner typed while Claude was working. Only queued input
whose origin is `human` is indexed; any other origin (task, peer, coordinator, auto-continuation, or none) and
chat-channel participants are skipped, and one flagged as meta is `harness`. Inside a subagent thread such a message
follows the usual subagent rule and is `delegation`.
`reply` an assistant message; `tool_call` a tool invocation (excluded from
default search, never citable); `harness` provider-injected text; `delegation` a message from a parent into a subagent
thread; `tool_error` redacted head and tail of an error-bearing tool output (never pushed, never citable).
**Thread classes:** `primary` (searched by default; this includes a Codex desktop session that continues a ChatGPT
conversation, `thread_source` `chatgpt_handoff`, whose opening message, the pasted conversation and the request ChatGPT
composed, is stored as `harness`), `subagent`,
`reviewer`, `other` (unrecognised format, never guessed primary). **Providers:** `claude`, `codex`, `cursor`. **Source roots:** `claude-projects`, `codex-sessions`, `codex-archived`, `cursor-imports`.

## Knowledge ledger

`muninn know add --kind decision|fact|preference|procedure|lesson|constraint --text T --cite REF --quote Q [--supersedes K] [--global]`
records an entry; every entry needs a quote that is verbatim in a primary prompt, reply or tool call (a preference needs
a user prompt). `--quote Q` alone searches the caller's session prompts. `know retract K [--reason R]`;
`know list [--status current|superseded|retracted|erased|expired|all] [--kind K] [--tag T]... [--all-projects] [--scope-loop ID]`; `know show K` the entry with
its chain and log; `know check` re-verifies every citation (`ok`, `changed`, `missing`, `erased`, `problems`) and counts `expired` entries.
Entries are superseded or retracted, never edited.
Only text the person typed is pushed unprompted: an entry is pushed (SessionStart, recall) only if a live user-prompt
citation's quote backs the entry text, meaning the text is inside the quote or at least 80% of its words of three or
more characters occur in it. Otherwise it stays pull-only (still in `know list` and `search`) and `know add` answers
with a `pull_only` line; re-add it in the user's own words. A restricted or an expired entry also gets a `pull_only` line, but
a different one: it says the entry is restricted (or past its `valid_until`), because citing a user prompt would not change that.
The rule is read-time, so it also covers older entries.

Optional typed fields on `know add` (all additive; an old invocation is unchanged). Each bad value is refused with a stable
code and writes nothing:

| Flag | Meaning | Refusal code |
|---|---|---|
| `--confidence observed\|reported\|inferred` | how the claim is known; absent means not stated | `bad_confidence` |
| `--valid-until DATE` | `YYYY-MM-DD` (midnight UTC) or an ISO 8601 datetime; no zone means UTC; must be after now, and a value equal to now is refused | `bad_valid_until` |
| `--sensitivity normal\|restricted` | default `normal` | `bad_sensitivity` |
| `--contradicts K` | informational link to an existing entry, like `--supersedes` but it changes nothing and logs no action | `bad_contradicts` |
| `--tag T` (repeatable) | retrieval tag, lowercase `[a-z0-9_-]`, 1 to 32 characters, at most 10 tags (the count is checked on what was given, before duplicates are dropped, so 10 distinct plus a repeat is refused); stored sorted and unique, as data, not searched | `bad_tags` |
| `--scope-loop ID` | store in the scope of a loop (`[A-Za-z0-9_.-]`, up to 64 characters) instead of the repo; not with `--global`. `know list --scope-loop ID` lists it; a loop scope is a `dir` scope keyed `loop:<ID>` and is never in the repo or global lists or pushed by hooks | `bad_loop_scope` |

Entries print `confidence`, `valid_until` (UTC ISO or null), `expired`, `sensitivity`, `contradicts` and `tags`.

**Expiry is derived, never written.** A current entry whose `valid_until` has passed is reported `expired: true` and is not
current: `know list` (default `--status current`), search, the SessionStart block and recall leave it out. Its row and status are
untouched; `know list --status expired` shows it, `--status all` and `know show` show it flagged, `know check` reports an
`expired` count, and `stats` counts it under `knowledge.expired` instead of `knowledge.current`.

**Withheld is counted.** The `hook session-start` line in `calls.jsonl` carries `withheld: {"expired": N, "restricted": N}`
(counts of current entries in the repo and global scopes that the block left out, never text; the field passes the
`obs_log` allowlist as a map of integer counts), so an empty block with a non-zero count is a decision the hook withheld,
not an empty ledger. An entry that is both expired and restricted counts as `expired` only. The converse does not hold: an
entry that is pull-only for lack of a backing user citation is in neither counter, so a zero count does not mean everything
in scope was pushed. If the count cannot be computed because of a store fault the field is left out and the block is still
sent; any other failure reaches the hook's outer handler and gives the `error` notice.
`know list --scope-loop` refuses a malformed id with `bad_loop_scope`, as `know add` does; a well-formed id that has never
had an entry lists 0 with `loop_not_found: true` (still exit 0, one JSON object). `--scope-loop` with `--all-projects` is
refused with `loop_with_all_projects` (exit 2).
`know list --tag T` (repeatable) keeps entries that carry every given tag, in any order, matched as whole tags (`run-1` does not
match `run-10`); it combines with `--kind`, `--status` and the scope flags, `count` is taken after it, and a tag that `know add`
would refuse gives `bad_tags` (exit 2). Search and the hooks have no tag filter.

**Restricted entries are never pushed.** A `restricted` entry is left out of the SessionStart block and of per-prompt
recall, even when a user prompt cites it. The commands someone runs on purpose show it: `know list`, `know show` and
`search`, where each `knowledge[]` hit carries `restricted: true|false`. An expired entry is not current, so `search` leaves
it out and reports how many it left out as `knowledge_expired_omitted` (absent when 0). A loop-scope entry is never in
SessionStart, recall or a repo-scoped search; `search --all-projects` drops the scope filter and does return it, as it does
for any other scope. `stats` reports `knowledge_restricted`, a count of entries that are current in the same sense as
`knowledge.current` (status `current` and not expired), so a restricted entry past its `valid_until` is counted under
`knowledge.expired` instead.

**Schema v2 is a one-way upgrade.** The first writing command (`know add`, `ingest`, the poller) on a v1 store rebuilds the
`knowledge` table once, in one transaction with `user_version` (ids, supersede chains, citations, the log and full-text
index are kept; a crash leaves the v1 file intact). Before it commits, the row count of the new table must equal the old
one and `PRAGMA foreign_key_check` must show no new violation, or the whole upgrade rolls back. If the commit, the rollback
or the pragma restore itself fails, the original error is the one reported. Each attempt writes one `poller.log` line:
`event` `migrated`, `migrate_raced` (another writer got there first) or `migrate_failed`, with `from_v`, `to_v`, `rows`
(rows copied; absent unless it migrated), `ms` and, for a failure, `exc` (the exception class name, never its message). After that an older muninn release refuses the store as a schema mismatch.
**`--upgrade` keeps this reversible.** Before it changes anything, an upgrade whose release has a higher `SCHEMA_VERSION` than the
store's `user_version` (it reads the number from `muninn/store_schema.py` at `--sha`, as text) copies the store with SQLite's
backup API to `muninn.sqlite.pre-upgrade-<12-hex sha>` (mode 0600, in the data dir); if the copy cannot be made or checked, or
the store cannot be read, the upgrade stops before anything else changes. If the upgrade then fails and rolls back, and the
store was migrated, the rollback stops the job, replaces `muninn.sqlite` with the copy (atomic rename, any journal beside it
removed first) and starts the old release on it; a store the new release never touched is kept and the copy deleted. A good
upgrade deletes the copy after the prune, because it holds transcript text; if the delete fails the installer warns, and
`doctor` warns `upgrade_snapshot` until it is removed. `install-record.json` records `snapshot` (name, byte size, `from` and
`to` versions, `state`: `deleted`, `restored` or `kept`), never content; `--check` names the copy and writes nothing.
Read-only callers (hooks, `stats`, `doctor`) refuse a v1 or v2 file until a writer has migrated it, so hooks fail open (they exit 0
with a `memory unavailable (store_unavailable)` notice) in that window. `muninn rebuild` accepts a v1 or v2 old store.

## Running and maintaining

| Command | What it does |
|---|---|
| `muninn ingest [--full]` | catch up with Claude and Codex provider transcripts now; `--full` rescans everything and imports Cursor history from `MUNINN_CURSOR_DB` when supplied, or `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` on macOS. Windows and Linux require an explicit database path; no layout is guessed. Cursor data is read-only and is not polled. Answer `ingest`: `files_seen`, `files_changed`, `events_added`, `events_removed`, `skipped_files`, `unreadable_files`, `skipped_lines`, `failed`, `errors`, `missing`, `duration_s` |
| `muninn serve [--interval S]` | the launchd poller loop (default 60 s); prints nothing |
| `muninn stats [--usage]` | counts, see below |
| `muninn doctor` | health checks, see below; exit 1 if any error-level check is `false` |
| `muninn compact` | VACUUM the database under the writer lock; answer `compact.bytes_before`/`bytes_after`, `db_space`; refuses without about one database of free disk |
| `muninn rebuild` | build a new store from the transcripts, then copy over what cannot be re-derived: scopes, tombstones, the knowledge ledger (entries, citations, log) and the events of sources that have gone missing. A store that cannot be read is never overwritten: it is renamed to `muninn.sqlite.unreadable-<UTC timestamp>` (mode 0600, only after checking for a hot journal, which exits 4) and the answer names it in `old_kept_as` (null otherwise) with a `warning` that its knowledge ledger, citations and scopes were not copied. If the new store cannot be moved into place the old one is renamed back (exit 4, `replace_failed`, `old_restored`); a missing or damaged `tombstone.key` stops the rebuild before anything is replaced |
| `muninn erase --session S \| --event REF \| --match TEXT [--dry-run] [--yes]` | forget content; without `--yes` it is a dry run. `--match` searches event text, knowledge text, tags, retract reasons and citation quotes. An erased knowledge entry also loses its tags, confidence, expiry, sensitivity and `contradicts` link (ids of other entries that point at it, scope and supersede links stay; a `loop:<id>` scope row stays too, it names a loop, not content). Writes tombstones so a rescan cannot restore it; each erased event also gets a content tombstone (a keyed HMAC of role and text, no text; see `tombstone.key`). It is applied only to a fork's replayed copy of its parent's history, never to a later identical turn, so erasing one `yes` does not drop the next. Copies already stored in the fork family are erased with it, whichever member the ref names: every matching event of an ancestor, and in a fork or sibling only the leading run of events that repeats its ancestors' history (matched by role and text), never the fork's own later turn. A dry run makes no `tombstone.key`. Answer also has `aside_files` and `aside_remove` (set-aside stores erase cannot scrub, including a leftover `muninn.sqlite.pre-upgrade-*` copy, and the command that removes them). An erase made without a content tombstone has only its line tombstone: a fork that copies the erased line byte for byte is still kept from storing it, including after a classifier change re-reads every transcript, but a copy that differs in any byte is not recognised and no re-read can add the missing tag. Lists provider files and other derived copies it cannot reach (`not_covered`, `out_of_scope`) |
| `muninn hook session-start\|prompt --provider claude\|codex` | provider hooks: payload on stdin, JSON on stdout (see the README) |

## Installing and removing (from a checkout)

| Command | What it does |
|---|---|
| `bin/muninn-install` | fresh install, or upgrade when installed (the last step moves each old release to `.pruning-<ts>-<sha>` so a failure can still roll back, then a sweep deletes those dirs after the point of no return; a sweep that fails warns, is listed as `sweep_failed` in the install record, and the next upgrade retries; `muninn doctor` warns with `release_leftovers` while a `.pruning-<ts>-<sha>` directory remains in `~/.local/lib/muninn`, which is where they live, not in the data directory); `--check` previews, `--status` compares the installed commit with `HEAD` |
| `bin/muninn-uninstall [--dry-run] [--purge-data]` | the same as `bin/muninn-install --uninstall`. Stops the poller; removes the launchd plist, our hooks from Claude's `settings.json`, our sections from Codex's `config.toml`, the Codex plugin cache, `~/.local/lib/muninn` and the `~/.local/bin/muninn` link (only when it points into that release directory). Moves the data directory to `~/.local/share/muninn-removed-<ts>/muninn` (`--purge-data` deletes it instead). `--dry-run` writes nothing. Prints one line per action, then `uninstall done`, `dry run only: nothing was removed` or `muninn is not installed here`, and exits 0. A provider config it cannot edit safely, a taken data-move name, or a job launchd will not unload prints `FAILED: <why>` and exits 1; the config and name checks run first, in a dry run too, before anything is changed |

## `muninn stats`

`sources` counts per `provider/root/thread_class/status` (status `active` or `missing`); `events` per kind;
`events_by_provider`; `flags` `marker` (text that looked like an injected block), `redacted` (secret removed), `truncated`
(over 64 KiB); `skipped_lines` and `issues` (lines not parsed: `line_too_large`, `invalid_json`, `too_deep`,
`not_object`); `other_threads` unrecognised threads by reason; `knowledge` by status (`current`, `expired`, `superseded`, `retracted`, `erased`) and `knowledge_restricted` (a count of current restricted entries); `citations` by state;
`tombstones` by level (`session`, `thread`, `line`); `db_bytes` file size; `db_space` `page_count`, `freelist_count`,
`page_size`, `free_ratio`; `last_pass` (`last_pass_at`, `duration_s`, `files_changed`, `events_added`, `skipped_files`,
`unreadable_files`, `last_error` (class name of the last failed pass, null after the poller's next good pass), `failed`, `errors`, `busy_skips`, `index_age_s`, `poller`, and `alive_age_s`, the seconds since a running pass last stamped
alive: null between passes and when no usable stamp exists, and growing if the poller was killed mid-pass and not
restarted); `reread`
(`pending` active sources still read under an older classifier version, `of` all active sources: the progress of the
one-time re-read after a classifier change; it normally falls to 0, and a source that fails or is skipped stays pending,
so look at `last_pass.failed`, `unreadable_files` and `skipped_files`); `install_sha`; `classifier_version`; `hash_mismatches` (open-time
line hash failures seen in the call log). `--usage` adds `usage` (per session `calls`, `errors`, `last_ts` of muninn calls seen
in transcripts) and `usage_totals` per provider.

## `muninn doctor`

Answer: `ok` (true when no error-level check is false) and `checks[]`. Each check has four fields:

- **`check`**: the check's name.
- **`ok`**: `true` passed; `false` failed; `null` could not be verified.
- **`level`**: how much a failure matters. `error`: `ok: false` makes the whole answer `ok: false` and the exit code 1.
  `warn`: shown, does not fail the run. `info`: informational, always `ok: true`.
- **`detail`**: a short string whose meaning depends on the check (below); empty when there is nothing to add.

| check | level | passes when | `detail` |
|---|---|---|---|
| `data_dir_mode` | error | the data dir is mode 0700 | its mode, or `absent` |
| `file_modes` | error | no data file is readable by group or others | names of loose files |
| `unexpected_files` | error | only known files are in the data dir | names of stray files |
| `unowned_journal` | error | no crashed writer's journal is lying around | explanation |
| `store_readable` | error | the database opens read-only and has the right schema | the refusal message if not, such as `schema v1, need v2` or `cannot read the store (SQLITE_BUSY)` |
| `journal_mode` | error | SQLite journal mode is `delete` | the mode |
| `fts_secure_delete` | error | both full-text indexes have secure delete on | none |
| `quick_check` | error | SQLite's `quick_check` says `ok` | its first 80 characters |
| `missing_sources` | info | always | count of indexed files no longer on disk |
| `other_threads` | info | always | count of unrecognised thread types |
| `reread` | info | always | `N of M`: active sources still to be re-read after a classifier change; failing sources are reported by `failed_sources` |
| `citations_resolve` | warn | every live knowledge citation still matches its original line | count that do not |
| `db_size` | warn | the database is under 2 GB | its size; the threshold |
| `db_free_space` | warn | free pages are under 25% or under 64 MB | free size and ratio; `run: muninn compact` |
| `writer_secure_delete` | error | writer connections turn secure delete on | none |
| `heartbeat` | error | the poller finished a pass, or a running pass stamped `alive_at`, within 3 intervals (`poller: ok`) | `index_age_s` |
| `failed_sources` | warn | no file failed in the last pass | count failed; the SessionStart block, and a recall block that has a hit, also say so |
| `poller_error` | warn | the last poller pass did not end in an exception; the poller's next good pass clears it, a manual `ingest` does not | the exception class name only |
| `unreadable_files` | warn | the last pass could open every transcript file | count of files it could not open (permissions; a file that vanished does not count); the SessionStart block, and a recall block that has a hit, also say so |
| `aside_files` | warn | no unreadable store set aside by `rebuild` is left in the data dir | names of `muninn.sqlite.unreadable-*` files |
| `upgrade_snapshot` | warn | no pre-upgrade copy of the store is left in the data dir | names of leftover `muninn.sqlite.pre-upgrade-*` files |
| `tombstone_key` | error | `tombstone.key` is whole (32 bytes), and present whenever keyed tombstones exist | `missing` or `damaged` |
| `release_leftovers` | warn | no `.pruning-*` release directory is left in `~/.local/lib/muninn` | their names |
| `launchd_job` | error | on macOS, the launchd job is loaded with a live process | its pid |
| `managed_service` | error | on Linux/Windows, the managed user service/task is owned and its heartbeat identifies the actual writer; unavailable inspection reports `null`, never success | constant code and process id |
| `roots_readable` | error | every provider root that exists is readable | names of blocked roots |
| `roots_present` | info | always | names of provider roots that do not exist |

## Per-prompt recall switch (`recall.off`)

**Function.** Before each prompt, the UserPromptSubmit hook (`muninn hook prompt`) can add a short block of up to 1,500
characters: matching knowledge entries first, then up to 3 earlier prompts or replies from this repo. If a file named
`recall.off` exists in the data directory, that hook prints `{}` and never opens the database, so no recall is added.
SessionStart (`muninn hook session-start`) is not affected; `MUNINN_HOOK_DISABLE=1` silences both hooks. Its block opens with
a usage line, identical for Claude Code and Codex, that tells an agent how to search, open a hit and record an owner
decision with `muninn know add --kind decision --text … --cite REF --quote "<verbatim>"`.

The hook output also carries a top-level `systemMessage` for the person at the keyboard (Claude Code shows it in the
transcript; Codex records it as a `warning` hook entry). At SessionStart it lists every pushed knowledge entry's headline
(its `text`, in full, control characters replaced by spaces), one per line under `muninn: memory loaded (N knowledge
entries)`; when the store cannot be read it is `muninn: memory unavailable (<code>)`. The entry text is never written to the
call log. Per-prompt recall adds no line.

**Default for new installs: OFF.** `--fresh` creates `recall.off` (mode 0600) and prints how to turn recall on. `--upgrade`
never creates or removes it, so an existing machine keeps whatever it had: a machine installed before this default has no
file and stays on.

**Why off.** Recall is the one memory muninn pushes without being asked, and a block about the wrong project costs the most
there. Its relevance has not been re-checked, so a new install starts with it off until the retrieval re-check passes.
Full reasoning is in `docs/adr/0007-recall-off-by-default.md`.

| To | Run |
|---|---|
| turn recall on | `unlink ~/.local/share/muninn/recall.off` |
| turn recall off | `install -m 600 /dev/null ~/.local/share/muninn/recall.off` |
| check | `ls ~/.local/share/muninn/recall.off` (present = off) |

The file must be mode 0600, otherwise `muninn doctor` fails `file_modes`. Its contents are ignored. The change takes effect on
the next prompt; no restart is needed.

## Files in the data directory

`muninn.sqlite` the database; `writer.lock` the writer lock; `status.json` the poller heartbeat (counts only; `last_error` and `last_error_at` name the class and time of a failed pass and are both cleared by the poller's next good pass (a manual `ingest` does not clear them); `alive_at` is refreshed every few seconds while a pass runs, so a long
re-read after a classifier change still shows `poller` `ok` while `index_age_s` keeps counting from the last finished pass);
`calls.jsonl` and `calls.jsonl.1` one allowlisted line per CLI call (ids and counts, no text; rotated at 1 MiB);
`poller.log` and `poller.log.1` poller events (rotated likewise; the plist sends launchd's own stdout and stderr to `/dev/null`, and a poller whose plist still sends them to the log silences them itself, so nothing reaches the log except allowlisted lines; launchd restarts the poller on any exit, a clean stop included, so only `launchctl bootout` keeps it down); `tombstones.jsonl` erase records; `tombstone.key` (exactly 32 random bytes, mode 0600, created whole the first time an erase needs it and never by a dry run, survives `rebuild`, never logged) the key that makes the content tombstones in `tombstones.jsonl` HMAC-SHA256 tags (`ev2:`) instead of plain hashes, so an erased short text cannot be confirmed by guessing. A new key is never made while keyed tombstones exist, because it would stop them matching: if the file is missing or damaged then, `ingest`, `rebuild` and `erase` exit 4 with `tombstone_key` and `doctor` fails `tombstone_key`; restore the file from a backup; `muninn.sqlite.unreadable-<UTC timestamp>` a store `rebuild` set aside because it could not be read. `erase` cannot scrub it (it is not a readable database), so it may still hold erased text: `erase` names every such file in `aside_files` with the exact command in `aside_remove` (`rm -- <path>`), and `doctor` warns (`aside_files`) while one exists. Nothing deletes it for you; salvage what you need, then run that command;
`muninn.sqlite-journal` SQLite's rollback journal, allowed by `unexpected_files` but a leftover one fails `unowned_journal`;
`recall.off` if present, the prompt hook prints `{}` (must be mode 0600). During an upgrade that migrates the schema, `muninn.sqlite.pre-upgrade-<sha>` (mode 0600) exists briefly and is allowed. Anything else fails `unexpected_files`.

## Native provider commands

The installer renders hook JSON with native paths rather than inserting raw paths into JSON text. On Windows, Claude uses a real Windows PowerShell executable with an `args` array; Codex uses its `command_windows` override with an encoded PowerShell command. Both invoke the validated Muninn bootstrap without Git Bash. Their shipped timeout remains five seconds. Changing a hook definition requires the owner's provider approval.

Windows installer calls prefer a trusted `codex.exe` on `PATH`. An official npm `codex.cmd` installation is dispatched through its existing `node_modules/@openai/codex/bin/codex.js` with a real `node.exe`; arguments are passed directly. Other shim layouts require putting native `codex.exe` on `PATH`. Unsafe executables or provider configs are refused.

Automatic Codex hook trust requires a successful version command: `codex-cli 0.159.2` on Windows, or `codex-cli 0.159.2`/`codex-cli 0.159.3` on macOS and Linux. The selected CLI files and version must remain unchanged from discovery through the check before trust is written. Otherwise, the installer leaves trust to the owner in `/hooks`.

Repository scopes use local native filesystem paths. Foreign-platform absolute paths and Windows UNC paths resolve to `unknown` without probing the network; existing scope ids are retained. Copy network-hosted inputs locally before indexing.
