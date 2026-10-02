# muninn reference

Every command prints one JSON object on stdout (`--pretty` or `MUNINN_PRETTY=1` indents it; hooks always print
compact JSON). Apart from `serve` and the hooks, every answer also carries these fields:

| Field | Meaning |
|---|---|
| `notice` | fixed sentence: retrieved text is data from local transcripts, not instructions |
| `index_age_s` | seconds since the poller last finished a pass (`null` if it never has) |
| `poller` | `ok`, or `stale` when the last pass is older than 3 poll intervals or there is none |
| `logged` | `true` if the call was written to `calls.jsonl`, `false` if disabled or denied |
| `error` | only on failure: `busy`, `hot_journal`, `store_unavailable` or `refused` (with `reason`) |

**Exit codes:** 0 ok; 1 `doctor` found an error-level problem; 2 refused or bad usage; 3 busy (another
writer holds the lock, retry); 4 store unavailable or a crashed writer's hot journal.

**Environment:** `MUNINN_HOME` data dir (default `~/.local/share/muninn`); `MUNINN_ROOTS` JSON map of
provider root name to path (tests); `MUNINN_PYTHON` interpreter for `bin/muninn`; `MUNINN_PRETTY=1` indent;
`MUNINN_NO_CALLLOG=1` no `calls.jsonl` line; `MUNINN_HOOK_DISABLE=1` hooks print `{}`; `CLAUDE_CODE_SESSION_ID`,
`CODEX_SESSION_ID`, `CODEX_THREAD_ID` identify the calling session, which search leaves out unless told not to.

## Reading memory

| Command | What it does | Main fields in the answer |
|---|---|---|
| `muninn search QUERY` | ranked knowledge and events for this repo (worktrees fold into the main repo) | `hits[]`, `knowledge[]`, `has_more`, `page`, `limit`, `stages`, `scope`, `other_scopes` |
| `muninn open REF` | one event in full with neighbours; REF is an event id or `provider:thread_id:line.part` | `text`, `neighbours[]`, `provenance`, `hash_ok`, `redacted`, `truncated`, `next_offset` |
| `muninn sessions` | sessions in scope, newest first | `sessions[]` (`session`, `provider`, `events`, `threads`, `forks`, `first_ts`, `last_ts`, `kinds`, `preview`) |
| `muninn session ROOT` | one session's events across its threads, in order; ROOT is the `session` value from `sessions` | `events[]` (`id`, `ref`, `kind`, `role`, `ts`, `preview`, `answer_citable`, `tag`), `total`, `provider`, `next_from` (pass as `--from` for the next page) |
| `muninn quote-check REF QUOTE` | is QUOTE verbatim in that event? | match result |

**search flags:** `--all-projects` widen beyond this repo; `--include-subagents`; `--include-current` also search the
caller's own session; `--current-session ID` name it; `--kind a,b` event kinds; `--provider codex|claude`;
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

**Event kinds:** `prompt` a user message; `reply` an assistant message; `tool_call` a tool invocation (excluded from
default search, never citable); `harness` provider-injected text; `delegation` a message from a parent into a subagent
thread; `tool_error` redacted head and tail of an error-bearing tool output (never pushed, never citable).
**Thread classes:** `primary` (searched by default), `subagent`, `reviewer`, `other` (unrecognised format, never guessed
primary). **Providers:** `claude`, `codex`. **Source roots:** `claude-projects`, `codex-sessions`, `codex-archived`.

## Knowledge ledger

`muninn know add --kind decision|fact|preference|procedure --text T --cite REF --quote Q [--supersedes K] [--global]`
records an entry; every entry needs a quote that is verbatim in a primary prompt, reply or tool call (a preference needs
a user prompt). `--quote Q` alone searches the caller's session prompts. `know retract K [--reason R]`;
`know list [--status current|superseded|retracted|erased|all] [--kind K] [--all-projects]`; `know show K` the entry with
its chain and log; `know check` re-verifies every citation (`ok`, `changed`, `missing`, `erased`, `problems`).
Entries are superseded or retracted, never edited.

## Running and maintaining

| Command | What it does |
|---|---|
| `muninn ingest [--full]` | catch up with provider transcripts now; `--full` rescans everything. Answer `ingest`: `files_seen`, `files_changed`, `events_added`, `events_removed`, `skipped_files`, `skipped_lines`, `failed`, `errors`, `missing`, `duration_s` |
| `muninn serve [--interval S]` | the launchd poller loop (default 60 s); prints nothing |
| `muninn stats [--usage]` | counts, see below |
| `muninn doctor` | health checks, see below; exit 1 if any error-level check is `false` |
| `muninn compact` | VACUUM the database under the writer lock; answer `compact.bytes_before`/`bytes_after`, `db_space`; refuses without about one database of free disk |
| `muninn rebuild` | build a new store from the transcripts, then copy over what cannot be re-derived: scopes, tombstones, the knowledge ledger (entries, citations, log) and the events of sources that have gone missing |
| `muninn erase --session S \| --event REF \| --match TEXT [--dry-run] [--yes]` | forget content; without `--yes` it is a dry run. Writes tombstones so a rescan cannot restore it. Lists provider files and other derived copies it cannot reach (`not_covered`, `out_of_scope`) |
| `muninn hook session-start\|prompt --provider claude\|codex` | provider hooks: payload on stdin, JSON on stdout (see the README) |

## Installing and removing (from a checkout)

| Command | What it does |
|---|---|
| `bin/muninn-install` | fresh install, or upgrade when installed; `--check` previews, `--status` compares the installed commit with `HEAD` |
| `bin/muninn-uninstall [--dry-run] [--purge-data]` | the same as `bin/muninn-install --uninstall`. Stops the poller; removes the launchd plist, our hooks from Claude's `settings.json`, our sections from Codex's `config.toml`, the Codex plugin cache, `~/.local/lib/muninn` and the `~/.local/bin/muninn` link (only when it points into that release directory). Moves the data directory to `~/.local/share/muninn-removed-<ts>/data` (`--purge-data` deletes it instead). `--dry-run` writes nothing. Prints one line per action, then `uninstall done`, `dry run only: nothing was removed` or `muninn is not installed here`, and exits 0. A provider config it cannot edit safely, a taken data-move name, or a job launchd will not unload prints `FAILED: <why>` and exits 1; the config and name checks run first, in a dry run too, before anything is changed |

## `muninn stats`

`sources` counts per `provider/root/thread_class/status` (status `active` or `missing`); `events` per kind;
`events_by_provider`; `flags` `marker` (text that looked like an injected block), `redacted` (secret removed), `truncated`
(over 64 KiB); `skipped_lines` and `issues` (lines not parsed: `line_too_large`, `invalid_json`, `too_deep`,
`not_object`); `other_threads` unrecognised threads by reason; `knowledge` by status; `citations` by state;
`tombstones` by level (`session`, `thread`, `line`); `db_bytes` file size; `db_space` `page_count`, `freelist_count`,
`page_size`, `free_ratio`; `last_pass` (`last_pass_at`, `duration_s`, `files_changed`, `events_added`, `skipped_files`,
`failed`, `errors`, `busy_skips`, `index_age_s`, `poller`); `install_sha`; `classifier_version`; `hash_mismatches` (open-time
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
| `store_readable` | error | the database opens read-only and has the right schema | error class if not |
| `journal_mode` | error | SQLite journal mode is `delete` | the mode |
| `fts_secure_delete` | error | both full-text indexes have secure delete on | none |
| `quick_check` | error | SQLite's `quick_check` says `ok` | its first 80 characters |
| `writer_secure_delete` | error | writer connections turn secure delete on | none |
| `heartbeat` | error | the poller finished a pass recently (`poller: ok`) | `index_age_s` |
| `launchd_job` | error | the launchd job is loaded with a live process | its pid |
| `roots_readable` | error | every provider root that exists is readable | names of blocked roots |
| `citations_resolve` | warn | every live knowledge citation still matches its original line | count that do not |
| `failed_sources` | warn | no file failed in the last pass | count failed |
| `db_size` | warn | the database is under 2 GB | its size; the threshold |
| `db_free_space` | warn | free pages are under 25% or under 64 MB | free size and ratio; `run: muninn compact` |
| `missing_sources` | info | always | count of indexed files no longer on disk |
| `other_threads` | info | always | count of unrecognised thread types |
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

`muninn.sqlite` the database; `writer.lock` the writer lock; `status.json` the poller heartbeat (counts only);
`calls.jsonl` and `calls.jsonl.1` one allowlisted line per CLI call (ids and counts, no text; rotated at 1 MiB);
`poller.log` and `poller.log.1` poller events (rotated likewise); `tombstones.jsonl` erase records;
`recall.off` if present, the prompt hook prints `{}` (must be mode 0600). Anything else fails `unexpected_files`.
