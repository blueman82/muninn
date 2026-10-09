![Muninn: a rune-hung raven in flight over a night sky](assets/muninn-banner-wide.png)
# Muninn: cross-provider memory and knowledge

**Muninn: Odin's raven of memory, for your Claude Code, Codex and Cursor history.**

It gives both one shared memory of past sessions plus a small ledger of cited knowledge: a Python
3.13 standard-library CLI (`muninn`) over one SQLite file, kept current by a background poller (launchd, systemd user service or Task Scheduler).
Everything it returns is untrusted historical data, never instructions. MIT licensed (`LICENSE`).

## Install, upgrade, uninstall, rollback

Needs macOS, Linux or Windows (x64, ARM64), Python 3.13+, git, and Claude Code and/or Codex for the hook integrations.
Windows is verified on hosted CI only: not with a non-admin token, on a desktop, or under WSL.
Cursor is optional; Muninn can import its local history database without Cursor installed.

    git clone https://github.com/blueman82/muninn && cd muninn
    bin/muninn-install --check   # plain-sentence preview, writes nothing
    bin/muninn-install           # fresh install, or upgrade if installed

- Upgrade: `git pull`, then the same two commands (`--status` compares the installed commit with
  `HEAD`). One pinned release is kept.
- Rollback: a failed install or upgrade restores itself, and puts back the pre-upgrade store copy if
  the new release had migrated it. After a good upgrade the old release is gone; check out an
  earlier commit and rerun.
- Codex: if the installer prints `OWNER STEP`, run `/hooks` and trust the two hooks.
- Uninstall: `bin/muninn-uninstall --dry-run` previews; `bin/muninn-uninstall` stops the poller and
  removes the hooks, the Codex plugin and the release. Your index and knowledge ledger are moved to
  `~/.local/share/muninn-removed-<ts>`; `--purge-data` deletes them instead.
- Templates: `integrations/` (Claude hooks, hooks-only Codex plugin) and `launchd/com.muninn.plist`
  (poller, `muninn serve --interval 60`). More: `docs/QUICKSTART.md`, `docs/REFERENCE.md`,
  `docs/TROUBLESHOOTING.md`.

## What it stores

- Primary threads: Codex user and `chatgpt_handoff` sessions (a handoff's opening message is
  `harness`), Claude Code main transcripts, and imported Cursor conversations. Only primary threads
  are searched, pushed and cited by default.
- Cursor conversations are imported read-only from the standard local database during fresh install
  and `muninn ingest --full`. Cursor is not required, and its database is not polled.
- Event kinds: `prompt`, `reply`, `tool_call`, tagged `harness` text, and `tool_error`, a redacted
  head and tail of an error-bearing tool output (never pushed and never citable).
- Subagent threads are also indexed, as class `subagent`, but stay out of default search, both hook
  blocks and preference citations; opt in with `--include-subagents`.
- Text is verbatim except secret spans (`[redacted:secret]`) and truncation above 64 KiB. Redaction
  is best-effort and can miss an unusual secret.

## What it never stores

- Codex `developer` records and Claude `attachment` records (where hook contexts land; the one
  exception is your own messages typed while Claude works, stored as prompts in the main thread),
  reasoning, and tool outputs other than the bounded `tool_error` summaries.
- Guardian and reviewer threads, and history replayed inside forks.
- Provider transcripts are only read, never modified.

## Commands

Apart from the hooks, output is one JSON object with a `notice` field, `index_age_s` and `poller:
ok|stale`. Exit codes: 0 ok, 1 `doctor` found an error-level problem, 2 refused or bad usage, 3
busy, 4 store unavailable, a crashed writer's hot journal, or a missing or damaged `tombstone.key`.
`muninn --help` lists flags and env vars. Skills: `docs/SKILLS.md`.

- `muninn search QUERY`: ranked knowledge and events for the current repository scope (worktrees
  fold into their main repo). `--all-projects` widens it; the caller's own session is excluded
  unless `--include-current`.
- `muninn open ID|REF`: the exact stored text with provenance and source-order neighbours
  (`--context N`, `--offset C`). `REF` is `provider:thread_id:line.part`; `--raw` returns the raw
  JSONL line with its `line_sha256` and `hash_ok`.
- `muninn sessions` and `muninn session ROOT`: sessions in scope, newest first, and one session's
  events across its threads.
- `muninn know add|retract|list|show|check`: the knowledge ledger. Every entry needs at least one
  `--cite REF --quote Q` whose quote is verbatim in a primary prompt, reply or tool call (a
  preference needs a user prompt). Entries are superseded or retracted, never edited. `know add`
  also takes `--confidence`, `--valid-until`, `--sensitivity`, `--contradicts`, `--tag` and
  `--scope-loop`; `know list` filters by `--tag` and `--status`. Expired, restricted and loop-scope
  entries are never pushed.
- `muninn quote-check REF QUOTE`: check a quote against an event.
- `muninn erase --session S | --event REF | --match TEXT`: forget content. It is a dry run that
  lists the targets unless you add `--yes` (`--dry-run` forces a dry run).
- `muninn stats` and `muninn doctor`: counts, database size and free space, and health checks.
  `muninn compact` vacuums the database; `muninn rebuild` builds a new store from the transcripts,
  keeping ledger and tombstones. `--pretty` (or `MUNINN_PRETTY=1`) indents any JSON.
- `muninn ingest`, `muninn serve`, `muninn hook ...`: catch-up ingest, the poller loop, and the
  provider hooks below.

Data lives in `$MUNINN_HOME` (default `~/.local/share/muninn`). `MUNINN_ROOTS` overrides the
provider source roots for tests and evals, and `MUNINN_HOOK_DISABLE=1` silences both hooks.

## Hook injections

These are the only automatic push, installed for Claude Code and Codex. Both redact secrets, escape
the frame delimiter, stay silent for subagent and reviewer transcripts, and fail open: on any error
they exit 0 with a bounded "store unavailable" notice (or `{}` when disabled). A stale poller (no
finished pass or alive stamp within 3 intervals), a failed or errored last pass, or unreadable transcript files
is flagged in the hook block; every CLI answer carries the poller's state.

- SessionStart (`muninn hook session-start --provider claude|codex`): at most 4,000 characters
  framed `<muninn-memory source="muninn" trust="untrusted-data">`. It lists up to 8 current
  knowledge entries that cite a user prompt whose words back the entry text (repository plus global
  scope), each with its actor and first verbatim quote. A usage line says to search before answering
  about earlier work and how to record an owner decision with `muninn know add`; a hint names
  `muninn open`. `calls.jsonl` counts the expired and restricted entries it withheld.
- UserPromptSubmit (`muninn hook prompt --provider claude|codex`): at most 1,500 characters framed
  `<muninn-memory source="muninn" trust="untrusted-data" kind="recall">`. It holds matching
  knowledge first, then up to 3 prompt or reply events from the repository scope (never the caller's
  own session), each with provider, role, kind, session, time, `ref` and a snippet of at most 300
  characters. Skipped for prompts with fewer than 3 query terms and prompts starting `/`.

Per-prompt recall switch: if `recall.off` exists in the data directory, the UserPromptSubmit hook
prints `{}` without opening the database; SessionStart is unaffected and shows you the entries it
pushed (a `systemMessage` line). Default OFF: `--fresh` creates it (`--upgrade` never does). To turn
it on: `docs/REFERENCE.md`.

`<muninn-memory` and `<muninn-recall` are the only automatic-injection markers. Ingest flags any
stored text that contains either (or the CLI notice sentence), so a pasted block never returns as a
normal prompt; hook contexts themselves are never stored.

## Privacy

- The data directory is mode 0700 and its files 0600. The CLI and poller run with `umask 077`, and
  the launchd job sets `Umask` "077".
- Logs never hold transcript text: `calls.jsonl` is an allowlist (a query is kept only as a term
  count and a hash prefix, plus counts of withheld entries), `status.json` holds counts and a bare
  error class name with its time, and `poller.log` holds event codes, counts and exception class
  names.
- Best-effort secret redaction at ingest and on every output.
- `muninn erase` writes tombstones, checked before any line is parsed, so a rescan, restart or
  archive move cannot bring erased content back. Content tombstones are keyed HMACs: `tombstone.key`
  (32 random bytes, made on the first real erase) must be restored from a backup if lost, since a
  new key stops existing tombstones matching; ingest, rebuild and erase exit 4 without it. Erase
  uses secure delete on the tables and the full-text index, scans the data directory for residue,
  and removes fork copies of an erased line. Provider transcripts, backups and free disk blocks are
  out of its reach; `erase` lists provider files and copies.
- `muninn.sqlite.unreadable-*` (a store `rebuild` set aside) and `muninn.sqlite.pre-upgrade-*` (kept
  during an upgrade) can hold transcript text. Erase reports them as `aside_files` with the command
  in `aside_remove`, and `doctor` warns.

## Development

- Runtime: Python 3.13 standard library only, launched by `bin/muninn` with `python3.13 -I -B`. No
  third-party imports and no MLX.
- Dev checks (`docs/STANDARDS.md`): the tests, then the whole gate (ruff, black, pyright strict,
  shellcheck and the stdlib rules):

      python3.13 -m unittest discover -s tests -t .
      python3.13 -m tools.check --full

- Tests use a temporary `MUNINN_HOME` and synthetic fixtures only.
