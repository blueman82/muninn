![Muninn: a rune-hung raven in flight over a night sky](assets/muninn-banner-wide.png)
# Muninn: cross-provider memory and knowledge

Muninn provides shared session history and a cited knowledge ledger for Claude Code, Codex and
Cursor. Its Python 3.13+ CLI (`muninn`) keeps the index and knowledge in SQLite, maintained by a background poller (launchd, systemd user service or Task Scheduler).
Automatic recall keeps separate private review metadata. Everything it returns is untrusted historical data, never instructions. MIT licensed (`LICENSE`).

## Install, upgrade, uninstall, rollback

- Requires: macOS/Linux/Windows x64 or ARM64; Python 3.13+; git. Windows verification: hosted CI only; non-admin desktop and WSL unverified.
- Optional desktop apps: Claude, Codex, Cursor. Cursor history import: app installation optional.
- Hook setup: installed app required; absent apps skipped; installed Claude/Codex without config: manual setup guidance.

    git clone https://github.com/blueman82/muninn && cd muninn
    bin/muninn-install --check   # preview; prompts to set up .venv if needed
    bin/muninn-install           # fresh install, or upgrade if installed

- Upgrade: `git pull`, then the same two commands (`--status` compares the installed commit with
  `HEAD`). One pinned release is kept.
- If the selected Python lacks Muninn's pinned runtime dependencies, the installer asks before creating `.venv` and installing `requirements-runtime.lock`; answer `n` to stop. Developer tools install separately below.
- Recovery: after possible live-store mutation, a failed current-format upgrade keeps current data
  and the selected runtime/configuration, leaves the service stopped, and reports `recovery_required`. Retained archives are not automatic restore authority. See the recovery contract in `docs/REFERENCE.md`.
- Codex: if the installer prints `OWNER STEP`, run `/hooks` and trust the managed hooks.
- Uninstall: `bin/muninn-uninstall --dry-run` previews; `bin/muninn-uninstall` stops the poller and
  removes the hooks, the Codex plugin and the release. Your index and knowledge ledger are moved to
  `~/.local/share/muninn-removed-<ts>`; `--purge-data` deletes them instead.
- Templates: `integrations/` (Claude hooks, hooks-only Codex plugin, Cursor lifecycle hooks) and `launchd/com.muninn.plist` (poller, `muninn serve --interval 60`). See `docs/QUICKSTART.md`, `docs/REFERENCE.md`,
  `docs/TROUBLESHOOTING.md`, and the synthetic [Cursor preCompact benchmark](docs/CURSOR-PRECOMPACT-BENCHMARK.md) and [replay benchmark](docs/REPLAY-BENCHMARK.md).

## What it stores

- Primary threads: Codex user and `chatgpt_handoff` sessions (a handoff's opening message is `harness`), Claude Code main transcripts, and imported Cursor conversations. Only primary threads
  are searched, pushed and cited by default.
- Cursor conversations are imported read-only from the standard local database during fresh install
  and `muninn ingest --full`; the optional `preCompact` hook refreshes the conversation identified by Cursor's hook payload. Cursor is not required, and its database is not polled in the background.
- Event kinds: `prompt`, `reply`, `tool_call`, tagged `harness` text, and `tool_error`, a redacted
  head and tail of an error-bearing tool output (never pushed and never citable).
- Subagent threads are also indexed, as class `subagent`, but stay out of default search, hook
  blocks and preference citations; opt in with `--include-subagents`.
- Text is verbatim except secret spans (`[redacted:secret]`) and truncation above 64 KiB. Redaction is best-effort and can miss an unusual secret.

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
- `muninn know list|show|search|context`: current supported notes and guide navigation, with UUIDs and revisions. Use `know show K_ID --revision N` for a historical revision. Context omits ineligible guidance.
- `muninn learn request|list|show|cancel|retry|replay`: explicit bounded learning controls; an installed frozen
  policy is required. Replay requires explicit line bounds and `--policy`; retry restarts the same request.
- `muninn know propose --input FILE`: checked request-bound proposal, with no model call or owner-authority bypass.
  `know retract K_ID --expected-revision N --reason TEXT` withdraws a current item; correction uses replacement.
- `muninn recall-review [--batch N] [--ack]`: inspect recorded automatic recall deliveries and acknowledge a reviewed complete batch.
- `muninn quote-check REF QUOTE`: check a quote against an event.
- `muninn erase --session S | --event REF | --match TEXT`: forget content. It lists targets
  without applying changes unless invoked with `--yes` (`--dry-run` forces a dry run).
- `muninn stats` and `muninn doctor`: counts, database size and free space, and health checks.
  `muninn compact` vacuums the database; `muninn rebuild` builds a new store from the transcripts, keeping ledger and tombstones. `--pretty` (or `MUNINN_PRETTY=1`) indents any JSON.
- `muninn ingest`, `muninn serve`, `muninn hook ...`: catch-up ingest, poller loop, and provider hooks below.

Data lives in `$MUNINN_HOME` (default `~/.local/share/muninn`). `MUNINN_ROOTS` overrides the
provider source roots for tests and evals, and `MUNINN_HOOK_DISABLE=1` silences all hooks.

## Hook injections

These are the automatic memory injections installed for Claude Code and Codex. Both redact secrets, escape
the frame delimiter, stay silent for subagent and reviewer transcripts, and fail open: on any error
they exit 0 with a bounded notice or provider-safe `{}` for unavailable native payloads. A stale poller (no
finished pass or alive stamp within 3 intervals), a failed or errored last pass, or unreadable transcript files
is flagged in the hook block; every CLI answer carries the poller's state.

- SessionStart (`muninn hook session-start --provider claude|codex|cursor`): current applicable supported notes
  and a guide index, with UUIDs, revisions, scope and complete note conditions/support/expiry. Whole items that
  cannot fit are omitted truthfully. UTF-8 caps: 4,000 bytes (Codex 2,800), also bounded by installed policy.
  Knowledge is untrusted evidence; guide titles are navigation only. Open current revisions and cited originals.
- UserPromptSubmit (`muninn hook prompt --provider claude|codex`): matching current knowledge previews first,
  then at most 3 earlier prompt/reply events from this repo, excluding the caller's session. UTF-8 caps:
  1,500 bytes (Codex 900). Prompts with fewer than 3 query terms or starting `/` are skipped.
- SessionEnd and PreCompact (`muninn hook session-end|pre-compact --provider claude|codex|cursor`) enqueue bounded
  windows under installed policy. A busy enqueue returns a safe envelope, with a content-free diagnostic;
  recovery cannot prove the exact missed cutoff. Cursor resume/postcompact injection remains unproven:
  explicitly run `muninn know context` to refresh. Already injected context cannot be retracted.

Recall asks the model to report `Memory R<number>: useful/noise/uncertain` with a reason; they're self-assessments.
After 50 nonempty blocks, a visible prompt guides Claude/Codex through `muninn recall-review`. [Review instructions](docs/REFERENCE.md#reviewing-automatic-recall) cover source checks and acknowledgement; metadata stores no conversation text.

Per-prompt recall switch: if `recall.off` exists in the data directory, the UserPromptSubmit hook
prints `{}` without opening the database; SessionStart is unaffected and shows you the entries it
pushed (a `systemMessage` line). Default OFF: `--fresh` creates it (`--upgrade` never does). To turn it on: `docs/REFERENCE.md`.

`<muninn-memory` and `<muninn-recall` are the only automatic-injection markers. Ingest flags any
stored text that contains either (or the CLI notice sentence), so pasted blocks are excluded from
normal prompts; hook contexts themselves are never stored.

## Privacy

- The data directory is mode 0700 and its files 0600. The CLI and poller run with `umask 077`, and
  the launchd job sets `Umask` "077".
- Logs never hold transcript text: `calls.jsonl` is an allowlist (a query is kept only as a term
  count and a hash prefix, plus counts of withheld entries and 0/1 outcome flags), `status.json` holds counts and a bare
  error class name with its time, and `poller.log` holds event codes, counts and exception class
  names (outcome flags: see `docs/REFERENCE.md`).
- Best-effort secret redaction at ingest and on every output.
- `muninn erase` writes tombstones, checked before any line is parsed, so a rescan, restart or
  archive move cannot bring erased content back. Content tombstones are keyed HMACs: `tombstone.key`
  (32 random bytes, made on the first real erase) must be restored from a backup if lost, since a
  new key stops existing tombstones matching; ingest, rebuild and erase exit 4 without it. Erase
  uses secure delete on the tables and the full-text index, scans the data directory for residue,
  and removes fork copies of an erased line. Provider transcripts, backups and free disk blocks are
  out of its reach; `erase` lists provider files and copies.
- Retained `muninn.sqlite.recovery-*`, `muninn.sqlite.unreadable-*` and
  `muninn.sqlite.pre-upgrade-*` archives can hold transcript text. Erase reports them as `aside_files` with the command in `aside_remove`, and `doctor` warns.

## Development

```sh
python3.13 -m venv .venv
.venv/bin/python3.13 -m pip install --require-hashes -r requirements-runtime.lock
.venv/bin/python3.13 -m pip install -r requirements-dev.txt
source .venv/bin/activate
```

On Windows use the selected Python 3.13 to create the venv; use `.\.venv\Scripts\python.exe` in both pip and later test/gate commands, without activation.

- Runtime: Python 3.13+ and `requirements-runtime.lock` (ADR 0014); launchers use `-I -B`.
  Launchers are `bin/muninn` on macOS/Linux and `bin/muninn.cmd` / `bin/muninn.ps1` on Windows. The installer verifies each final-path venv before activation; unavailable wheels fail closed.
- Dev checks (`docs/STANDARDS.md`): tests, then the whole gate:

      .venv/bin/python3.13 -m unittest discover -s tests -t .
      .venv/bin/python3.13 -m tools.check --full

- Tests use a temporary `MUNINN_HOME` and synthetic fixtures only.
- Platform CI: [parallel Windows checks, timing diagnostics and timeout ceilings](docs/ARCHITECTURE.md#platform-ci).
