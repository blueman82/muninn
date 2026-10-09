# Quick start

## Prerequisites

- macOS (launchd), Linux (systemd user service) or Windows x64/ARM64 (Task Scheduler).
- Python 3.13 or newer. The installer pins the interpreter it runs under; set
  `MUNINN_PYTHON=/path/to/python3.13` to override at run time.
- `git`, and a clean clone of this repo checked out at the commit to install.
- Claude Code and/or Codex. Either is optional: a missing `~/.claude/settings.json` or
  `~/.codex/config.toml` is skipped and reported.

## Install on a new machine

From the repository root, with a clean worktree at the required commit:

    bin/muninn-install --check    # plain-sentence preview; writes nothing
    bin/muninn-install

`bin/muninn-install` installs this checkout's `HEAD`. It runs a fresh install when
neither the data directory nor release exists, and an upgrade when both exist. An
incomplete installation (only one exists) is refused. `--status` compares the installed
commit with `HEAD`. Other arguments are forwarded to `install.installer` (`--repo`,
`--sha`, `--fresh`, `--upgrade`, `--dry-run`, `--home`), which needs a full 40-hex
`--sha` equal to `HEAD`. A fresh install refuses if the data directory or the launchd
plist already exists. It pins the release, indexes your existing transcripts, starts the
poller, merges the two hooks into Claude's settings, adds the Codex plugin, runs
`muninn doctor`, and prunes to one release. Any failure rolls back. An upgrade that will
migrate the store first copies it aside (deleted after a successful upgrade, restored
after a failed one).

Codex only runs plugin hooks after you trust them: start a Codex session and run
`/hooks` if the installer prints `OWNER STEP`.

## Upgrade an installed machine

    bin/muninn-install --check
    bin/muninn-install

The installer selects the commit, restarts the poller, verifies, then deletes every
other release. It keeps the existing index instead of running the fresh-install history
import; after a successful upgrade, Claude and Codex polling resumes, while Cursor
history is not re-imported. Provider config is not touched. When a release changes how
transcripts are classified (`stats` shows `classifier_version`), the poller re-reads
every transcript once after the upgrade, Claude and Codex alike, one source at a time;
large stores may require additional time. Monitor progress with `muninn stats`
(`reread.pending` falls towards 0) or `muninn doctor` (the `reread` line). The re-read
holds the writer lock longer, so other writers see exit 3 meanwhile, and it can leave
free pages: if `doctor` then warns `db_free_space`, run `muninn compact`. A failed
upgrade returns to the old release; after a successful one the old release is removed
(check out an earlier commit and run `bin/muninn-install` to go back).

## Uninstall

    bin/muninn-uninstall --dry-run    # says what it would do; writes nothing
    bin/muninn-uninstall

It stops the poller, removes the launchd plist, removes only Muninn's hooks from
`~/.claude/settings.json` and only Muninn's sections from `~/.codex/config.toml`,
deletes the Codex plugin cache, and removes `~/.local/lib/muninn` and the
`~/.local/bin/muninn` link (only if it points into that release directory). Your
transcripts and every other setting stay as they are. If a provider config cannot be
edited safely it refuses before mutation, reports the cause and exits 1. Resolve the
reported cause before retrying.

The data directory (the index and the knowledge ledger) is moved to
`~/.local/share/muninn-removed-<timestamp>/muninn`, not deleted, because the ledger
cannot be rebuilt from transcripts. Add `--purge-data` to delete it. Repeated runs and
runs on an uninstalled machine report the existing state without further changes. After
an uninstall `bin/muninn-install` does a fresh install.

## Per-prompt recall starts off

A `--fresh` install creates `~/.local/share/muninn/recall.off`, so muninn does not add
earlier prompts to each of your prompts. Session-start memory and `muninn search` remain
available. Automatic recall is disabled by default pending retrieval relevance
validation (`docs/adr/0007-recall-off-by-default.md`).

To turn recall on: `unlink ~/.local/share/muninn/recall.off`. To turn it off again:
`install -m 600 /dev/null ~/.local/share/muninn/recall.off`. Details in
`docs/REFERENCE.md`. An `--upgrade` never changes the file.

## Verify the installation

    muninn doctor            # exit 0 = healthy
    muninn --pretty stats    # counts; add --pretty for indented JSON
    muninn search "a phrase you remember"

## Cursor history

On a fresh install, Muninn checks for Cursor's database at
`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` and imports it
when present. Cursor does not need to be installed. The installer prints which of
Claude, Codex and Cursor have history to index; `--dry-run` reports what it would index
without importing anything.

    muninn search "a phrase from Cursor" --provider cursor

## Installed files

| Path | What |
|---|---|
| `~/.local/lib/muninn/<sha>/` | the one pinned release |
| `~/.local/lib/muninn/current`, `python` | links to it and to the interpreter |
| `~/.local/lib/muninn/install-record.json` | sha, Python, config keys touched, outcome |
| `~/.local/lib/muninn/install.log` | timestamped steps and exit codes, no transcript text |
| `~/.local/share/muninn/` | the database, logs and heartbeat |
| `~/.local/bin/muninn` | link to `current/bin/muninn` |
