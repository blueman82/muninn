# Quick start

## Prerequisites

- macOS (the poller is a launchd job).
- Python 3.13 or newer. The installer pins the interpreter it runs under;
  set `PCTX_PYTHON=/path/to/python3.13` to override at run time.
- `git`, and a clean clone of this repo checked out at the commit to install.
- Claude Code and/or Codex. Either is optional: a missing
  `~/.claude/settings.json` or `~/.codex/config.toml` is skipped and reported.

## Install on a new machine

From the repo root (a clean worktree at the commit you want):

    bin/pctx-install --check    # plain-sentence preview; writes nothing
    bin/pctx-install

`bin/pctx-install` installs this checkout's `HEAD`. With nothing installed it
runs a fresh install; with pctx already installed it upgrades; if only half is
there (data directory without a release, or the reverse) it stops and says so.
`--status` compares the installed commit with `HEAD`. Any other arguments go
straight to `install.installer` (`--repo`, `--sha`, `--fresh`, `--upgrade`,
`--dry-run`, `--home`), which needs a full 40-hex `--sha` equal to `HEAD`.
A fresh install refuses if the data directory or the launchd plist already exists.
It pins the release, indexes your existing transcripts, starts the poller,
merges the two hooks into Claude's settings, adds the Codex plugin, runs
`pctx doctor`, and prunes to one release. Any failure rolls back.

Codex only runs plugin hooks after you trust them: start a Codex session and
run `/hooks` if the installer prints `OWNER STEP`.

## Upgrade an installed machine

    bin/pctx-install --check
    bin/pctx-install

It re-pins the commit, restarts the poller, verifies, then deletes every other
release. Provider config is not touched. A failed upgrade returns to the old
release; after a successful one the old release is gone (check out an earlier
commit and run `bin/pctx-install` to go back).

## Per-prompt recall starts off

A `--fresh` install creates `~/.local/share/provenance-context/recall.off`, so pctx does not add earlier prompts to each of
your prompts. Session-start memory and `pctx search` still work. A pre-release trial answered for the wrong project in some
test questions, so recall waits for a re-check (`docs/adr/0007-recall-off-by-default.md`).

To turn recall on: `unlink ~/.local/share/provenance-context/recall.off`. To turn it off again:
`install -m 600 /dev/null ~/.local/share/provenance-context/recall.off`. Details in `docs/REFERENCE.md`. An `--upgrade` never
changes the file.

## Check it works

    pctx doctor            # exit 0 = healthy
    pctx --pretty stats    # counts; add --pretty for indented JSON
    pctx search "a phrase you remember"

## What the installer leaves behind

| Path | What |
|---|---|
| `~/.local/lib/provenance-context/<sha>/` | the one pinned release |
| `~/.local/lib/provenance-context/current`, `python` | links to it and to the interpreter |
| `~/.local/lib/provenance-context/install-record.json` | sha, Python, config keys touched, outcome |
| `~/.local/lib/provenance-context/install.log` | timestamped steps and exit codes, no transcript text |
| `~/.local/share/provenance-context/` | the database, logs and heartbeat |
| `~/.local/bin/pctx` | link to `current/bin/pctx` |
