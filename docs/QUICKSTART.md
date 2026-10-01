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

    python3.13 -E -s -B -m install.installer \
        --repo . --sha "$(git rev-parse HEAD)" --fresh --dry-run   # preview
    python3.13 -E -s -B -m install.installer \
        --repo . --sha "$(git rev-parse HEAD)" --fresh

`--fresh` refuses if the data directory or the launchd plist already exists.
It pins the release, indexes your existing transcripts, starts the poller,
merges the two hooks into Claude's settings, adds the Codex plugin, runs
`pctx doctor`, and prunes to one release. Any failure rolls back.

Codex only runs plugin hooks after you trust them: start a Codex session and
run `/hooks` if the installer prints `OWNER STEP`.

## Upgrade an installed machine

    python3.13 -E -s -B -m install.installer \
        --repo . --sha "$(git rev-parse HEAD)" --upgrade

It re-pins the commit, restarts the poller, verifies, then deletes every other
release. Provider config is not touched. A failed upgrade returns to the old
release; after a successful one the old release is gone (re-run `--upgrade`
at an earlier commit to go back).

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
