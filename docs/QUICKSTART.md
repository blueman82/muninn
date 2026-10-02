# Quick start

## Prerequisites

- macOS (the poller is a launchd job).
- Python 3.13 or newer. The installer pins the interpreter it runs under;
  set `MUNINN_PYTHON=/path/to/python3.13` to override at run time.
- `git`, and a clean clone of this repo checked out at the commit to install.
- Claude Code and/or Codex. Either is optional: a missing
  `~/.claude/settings.json` or `~/.codex/config.toml` is skipped and reported.

## Install on a new machine

From the repo root (a clean worktree at the commit you want):

    bin/muninn-install --check    # plain-sentence preview; writes nothing
    bin/muninn-install

`bin/muninn-install` installs this checkout's `HEAD`. With nothing installed it
runs a fresh install; with muninn already installed it upgrades; if only half is
there (data directory without a release, or the reverse) it stops and says so.
`--status` compares the installed commit with `HEAD`. Any other arguments go
straight to `install.installer` (`--repo`, `--sha`, `--fresh`, `--upgrade`,
`--dry-run`, `--home`), which needs a full 40-hex `--sha` equal to `HEAD`.
A fresh install refuses if the data directory or the launchd plist already exists.
It pins the release, indexes your existing transcripts, starts the poller,
merges the two hooks into Claude's settings, adds the Codex plugin, runs
`muninn doctor`, and prunes to one release. Any failure rolls back.

Codex only runs plugin hooks after you trust them: start a Codex session and
run `/hooks` if the installer prints `OWNER STEP`.

## Upgrade an installed machine

    bin/muninn-install --check
    bin/muninn-install

It re-pins the commit, restarts the poller, verifies, then deletes every other
release. Provider config is not touched. A failed upgrade returns to the old
release; after a successful one the old release is gone (check out an earlier
commit and run `bin/muninn-install` to go back).

## Uninstall

    bin/muninn-uninstall --dry-run    # says what it would do; writes nothing
    bin/muninn-uninstall

It stops the poller, removes the launchd plist, takes only muninn's hooks out
of `~/.claude/settings.json` and only muninn's sections out of
`~/.codex/config.toml`, deletes the Codex plugin cache, and removes
`~/.local/lib/muninn` and the `~/.local/bin/muninn` link (only if it points into
that release directory). Your transcripts and every other setting stay as they
are. If a provider config cannot be edited safely it stops before changing
anything, says why, and exits 1; fix that and run it again.

The data directory (the index and the knowledge ledger) is moved to
`~/.local/share/muninn-removed-<timestamp>/data`, not deleted, because the
ledger cannot be rebuilt from transcripts. Add `--purge-data` to delete it.
Running it again, or on a machine without muninn, changes nothing and says so.
After an uninstall `bin/muninn-install` does a fresh install.

## Per-prompt recall starts off

A `--fresh` install creates `~/.local/share/muninn/recall.off`, so muninn does not add earlier prompts to each of
your prompts. Session-start memory and `muninn search` still work. Recall pushes text nobody asked for and its
relevance has not been re-checked, so it waits for that check (`docs/adr/0007-recall-off-by-default.md`).

To turn recall on: `unlink ~/.local/share/muninn/recall.off`. To turn it off again:
`install -m 600 /dev/null ~/.local/share/muninn/recall.off`. Details in `docs/REFERENCE.md`. An `--upgrade` never
changes the file.

## Check it works

    muninn doctor            # exit 0 = healthy
    muninn --pretty stats    # counts; add --pretty for indented JSON
    muninn search "a phrase you remember"

## What the installer leaves behind

| Path | What |
|---|---|
| `~/.local/lib/muninn/<sha>/` | the one pinned release |
| `~/.local/lib/muninn/current`, `python` | links to it and to the interpreter |
| `~/.local/lib/muninn/install-record.json` | sha, Python, config keys touched, outcome |
| `~/.local/lib/muninn/install.log` | timestamped steps and exit codes, no transcript text |
| `~/.local/share/muninn/` | the database, logs and heartbeat |
| `~/.local/bin/muninn` | link to `current/bin/muninn` |
