![Muninn: a rune-hung raven in flight over a night sky](assets/muninn-banner-wide.png)
# Muninn: cross-provider memory and knowledge

**Muninn: Odin's raven of memory, for your Claude Code and Codex sessions.**

It gives both one shared memory of past sessions plus a small ledger of cited
knowledge: a Python 3.13 standard-library CLI (`muninn`) over one SQLite file,
kept current by a launchd poller. Everything it returns is untrusted historical
data, never instructions. MIT licensed (`LICENSE`).

## Install, upgrade, uninstall, rollback

Needs macOS, Python 3.13+, git, and Claude Code and/or Codex.

    git clone https://github.com/blueman82/muninn && cd muninn
    bin/muninn-install --check   # plain-sentence preview, writes nothing
    bin/muninn-install           # fresh install, or upgrade if installed

- Upgrade: `git pull`, then the same two commands (`--status` compares the
  installed commit with `HEAD`). One pinned release is kept.
- Rollback: a failed install or upgrade restores itself. After a successful
  upgrade the old release is gone; check out an earlier commit and rerun.
- Codex: if the installer prints `OWNER STEP`, run `/hooks` and trust the
  two muninn hooks.
- Uninstall: `bin/muninn-uninstall --dry-run` previews; `bin/muninn-uninstall`
  stops the poller and removes the hooks, the Codex plugin and the release.
  Your index and knowledge ledger are moved to
  `~/.local/share/muninn-removed-<ts>`; `--purge-data` deletes them instead.
- Templates: `integrations/` (Claude hooks, hooks-only Codex plugin) and
  `launchd/com.muninn.plist` (poller, `muninn serve --interval 60`). More:
  `docs/QUICKSTART.md`, `docs/REFERENCE.md`, `docs/TROUBLESHOOTING.md`.

## What it stores

- Primary threads: Codex user sessions (`thread_source=user`, live and
  archived) and Claude Code main transcripts. Only these are searched, pushed
  and cited by default.
- Event kinds: `prompt`, `reply`, `tool_call`, tagged `harness` text, and
  `tool_error`, a redacted head and tail of an error-bearing tool output
  (never pushed and never citable).
- Subagent threads are also indexed, as class `subagent`, but stay out of
  default search, both hook blocks and preference citations; opt in with
  `--include-subagents`.
- Text is verbatim except secret spans (`[redacted:secret]`) and truncation
  above 64 KiB.

## What it never stores

- Codex `developer` records and Claude `attachment` records (where hook
  contexts land; the one exception is your own messages typed while Claude
  works, stored as prompts in the main thread), reasoning, and tool outputs
  other than the bounded
  `tool_error` summaries.
- Guardian and reviewer threads, and history replayed inside forks.
- Provider transcripts are only read, never modified.

## Commands

Apart from the hooks, output is one JSON object with a `notice` field,
`index_age_s` and `poller: ok|stale`. Exit codes: 0 ok, 2 refused or bad
usage, 3 busy, 4 store unavailable. `muninn --help` lists flags and env vars.
Claude Code skills for these commands: `docs/SKILLS.md`.

- `muninn search QUERY`: ranked knowledge and events for the current repository
  scope (worktrees fold into their main repo). `--all-projects` widens it; the
  caller's own session is excluded unless `--include-current`.
- `muninn open ID|REF`: the exact stored text with provenance and source-order
  neighbours (`--context N`, `--offset C`). `REF` is
  `provider:thread_id:line.part`; `--raw` returns the raw JSONL line with its
  `line_sha256` and `hash_ok`.
- `muninn sessions` and `muninn session ROOT`: sessions in scope, newest first,
  and one session's events across its threads.
- `muninn know add|retract|list|show|check`: the knowledge ledger. Every entry
  needs at least one `--cite REF --quote Q` whose quote is verbatim in a
  primary prompt, reply or tool call (a preference needs a user prompt).
  Entries are superseded or retracted, never edited.
- `muninn quote-check REF QUOTE`: check a quote against an event.
- `muninn erase --session S | --event REF | --match TEXT`: forget content;
  `--dry-run` previews the targets without deleting.
- `muninn stats` and `muninn doctor`: counts, database size and free space, and
  health checks. `muninn compact` vacuums the database. `--pretty` (or
  `MUNINN_PRETTY=1`) indents any JSON.
- `muninn ingest`, `muninn serve`, `muninn hook ...`: catch-up ingest, the
  poller loop, and the provider hooks below.

Data lives in `$MUNINN_HOME` (default `~/.local/share/muninn`).
`MUNINN_ROOTS` overrides the provider source roots for tests and evals, and
`MUNINN_HOOK_DISABLE=1` silences both hooks.

## Hook injections

These are the only automatic push, installed for Claude Code and Codex. Both
redact secrets, escape the frame delimiter, stay silent for subagent and
reviewer transcripts, and fail open: on any error they exit 0 with a bounded
"store unavailable" notice (or `{}` when disabled). A stale poller (no finished pass
or alive stamp within 3 intervals) is flagged in the hook block and in every CLI
response.

- SessionStart (`muninn hook session-start --provider claude|codex`): at most
  4,000 characters framed
  `<muninn-memory source="muninn" trust="untrusted-data">`.
  It lists up to 8 current knowledge entries that cite a user prompt
  (repository plus global scope), each with its actor and first verbatim
  quote, and a usage line pointing at `muninn search` and `muninn open`.
- UserPromptSubmit (`muninn hook prompt --provider claude|codex`): at most
  1,500 characters framed
  `<muninn-memory source="muninn" trust="untrusted-data" kind="recall">`.
  It holds matching knowledge first, then up to 3 prompt or reply events from
  the repository scope (never the caller's own session), each with provider,
  role, kind, session, time, `ref` and a snippet of at most 300 characters.
  It is skipped for prompts with fewer than 3 query terms and prompts
  starting with `/`.

Per-prompt recall switch: if `recall.off` exists in the data directory, the
UserPromptSubmit hook prints `{}` without opening the database; SessionStart is
unaffected and shows you the entries it pushed (a `systemMessage` line).
Default OFF: `--fresh` creates it (`--upgrade` never does). To turn it on:
`docs/REFERENCE.md`.

`<muninn-memory` and `<muninn-recall` are the only automatic-injection markers.
Ingest flags any stored text that contains either (or the CLI notice
sentence), so a pasted block never returns as a normal prompt; hook contexts
themselves are never stored.

## Privacy

- The data directory is mode 0700 and its files 0600. The CLI and poller run
  with `umask 077`, and the launchd job sets `Umask` "077".
- Logs never hold transcript text: `calls.jsonl` is an allowlist (a query is
  kept only as a term count and a hash prefix), `status.json` holds counts,
  and `poller.log` holds event codes, counts and exception class names.
- Secrets are redacted at ingest and again on every output.
- `muninn erase` writes tombstones (identifiers and hashes only) that are
  checked before any line is parsed, so a rescan, restart or archive move
  cannot bring erased content back. It uses secure delete on the tables and
  the full-text index, then scans the data directory for residue and reports
  it. Provider transcripts, Time Machine and free disk blocks are out of its
  reach; `erase` prints the provider file paths and other derived copies.

## Development

- Runtime: Python 3.13 standard library only, launched by `bin/muninn` with
  `python3.13 -I -B`. No third-party imports and no MLX.
- Dev checks (`docs/STANDARDS.md`): the tests, then the whole gate (ruff,
  black, pyright strict, shellcheck and the stdlib rules):

      python3.13 -m unittest discover -s tests -t .
      python3.13 -m tools.check --full

- Tests use a temporary `MUNINN_HOME` and synthetic fixtures only.
