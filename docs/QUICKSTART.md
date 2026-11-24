# Quick start

## Prerequisites

- macOS (launchd).
- Python 3.13 or newer. The installer pins the interpreter that executes it.
  Set `MUNINN_PYTHON=/path/to/python3.13` to select a different interpreter at run time.
- `git`, and a clean clone of this repo at the commit to install.
- Optional desktop apps: Claude, Codex, Cursor.

The installer skips hook setup for a missing app.
For installed Claude/Codex without `~/.claude/settings.json`/`~/.codex/config.toml`, it gives manual setup guidance.

## Install on a new machine

From the repository root, with a clean worktree at the necessary commit:

```sh
bin/muninn-install --check    # preview in plain sentences. Writes nothing
bin/muninn-install
```

`--check` does not make an environment, install packages or enable Git hooks.
It uses a compatible selected interpreter or this checkout's prepared `.venv`.
If the pinned runtime is unavailable, prepare it before trying again:

```sh
python3.13 -m venv .venv
.venv/bin/python3.13 -m pip install --require-hashes -r requirements-runtime.lock
```

You can also set `MUNINN_PYTHON` to an existing compatible environment.
A usual install can offer runtime and Git-hook setup.
Approval of those prompts changes the checkout.
That setup occurs independently of the read-only preview.

`bin/muninn-install` installs this checkout's `HEAD`.
It selects a new installation if there is no data directory and no release.
It selects an upgrade when the data directory and release exist.
It refuses an installation that has only one of them.

`--status` compares the installed commit with `HEAD` through local Git history.
It reports if the checkout or installed version is newer.
If the installed version is newer, update the checkout before installing.
Unknown or divergent history reports `versions differ`.
Examine the revisions before installing.
Status does not fetch from the network or change the installation.

Other arguments go to `install.installer`: `--repo`, `--sha`, `--fresh`, `--upgrade`, `--dry-run` and `--home`.
The installer must have a full 40-hex `--sha` equal to `HEAD`.
A new installation refuses an existing data directory or launchd plist.
It pins the release, indexes existing transcripts, configures managed provider hooks, and starts the poller.
It then runs `muninn doctor` and prunes to one release.

If an install stops with an error, the installer undoes its own changes.
After a possible change to the live store, an upgrade keeps the current data and the selected runtime and configuration.
It keeps the service stopped and records `recovery_required`.
Archives that remain give no automatic authority to replace current data.

Desktop app proof is `Claude.app`, `Codex.app` or `Cursor.app` in `/Applications` or `~/Applications`.

- Provider setup, config validation and hook checks must have an installed app.
- Leftover settings or transcripts do not prove installation.
- The installer skips setup for a missing app.
- Installed Claude/Codex without config means manual setup guidance.
- History import and an existing index operate independently of app detection.
- The Cursor setup prompt needs installed Cursor and an interactive install.

An interactive install gives two Cursor setup selections: automatic or manual.
Automatic setup merges only Muninn's `sessionStart`, `sessionEnd` and `preCompact` handlers into `~/.cursor/hooks.json` and keeps other hooks.
Rollback replaces prior owned handlers and keeps current new entries, edits, deletions and sequence from other software.
For a non-interactive install or manual selection, the installer does not change Cursor config.
It prints the target path and a direct hyperlink to the instructions below.
An upgrade changes Cursor settings only with automatic setup.

Codex runs plugin hooks only after your trust decision.
Start a Codex session and run `/hooks` if the installer prints `OWNER STEP`.

## Upgrade an installed machine

```sh
bin/muninn-install --check
bin/muninn-install
```

The installer selects the commit, starts the poller again, checks the result, and removes all other releases.
It keeps the existing index and does not run the new-installation history import.
After an upgrade that ends without error, Claude and Codex polling continues.
It does not import Cursor history again.
New installations and upgrades reconcile managed Claude/Codex definitions and keep unrelated hooks.
Cursor reconciliation follows the automatic setup selection.

Changed definitions must have owner approval.
Use `/hooks` for Codex when prompted.
When a release changes how it classifies transcripts, `stats` shows `classifier_version`.
After that upgrade, the poller reads each Claude and Codex transcript again, one source at a time.
Large stores can use more time.

Monitor the result with `muninn stats`: `reread.pending` decreases to 0.
You can also use the `reread` line in `muninn doctor`.
The new read holds the writer lock longer, so other writers see exit 3 during that time.
The store can then have free pages.
If `doctor` then reports `db_free_space`, run `muninn compact`.
An upgrade error can make recovery necessary with no rollback to the previous release, as described above.


## Uninstall

```sh
bin/muninn-uninstall --dry-run    # preview. Writes nothing
bin/muninn-uninstall
```

Uninstall stops the poller and removes the launchd plist.
It removes only Muninn's hooks from `~/.claude/settings.json`, managed lifecycle handlers from `~/.cursor/hooks.json`, and sections from `~/.codex/config.toml`.
It removes the Codex plugin cache and `~/.local/lib/muninn`.
It removes the `~/.local/bin/muninn` symbolic link only if it points into that release directory.
Your transcripts and all other settings stay unchanged.

If safe provider config edits are not possible, uninstall refuses before mutation, reports the cause and exits 1.
Resolve the reported cause before trying again.
It moves the data directory, with the index and knowledge ledger, to `~/.local/share/muninn-removed-<timestamp>/muninn`.
It does not remove the directory because transcripts cannot rebuild the ledger.
Add `--purge-data` to remove it.

Subsequent runs and runs on an uninstalled machine report the existing conditions without more changes.
After an uninstall, `bin/muninn-install` makes a new installation.

## Per-prompt recall starts off

A `--fresh` install makes `~/.local/share/muninn/recall.off`.
Thus muninn does not add earlier prompts to each of your prompts.
Session-start memory and `muninn search` remain available.
Automatic recall starts disabled, pending retrieval relevance validation (`docs/adr/0007-recall-off-by-default.md`).

Advisory mode gives the model a short `<muninn-retrieval>` instruction to examine project history when necessary.
It does not add automatic recall or a hook store read.
Make `~/.local/share/muninn/recall.advisory` with mode 0600 and remove `recall.off` to select it.
If the two files exist, off wins.

To turn recall on: `unlink ~/.local/share/muninn/recall.off`.
To turn it off again: `install -m 600 /dev/null ~/.local/share/muninn/recall.off`.
See `docs/REFERENCE.md`.
An upgrade does not change the two mode files.

With recall enabled, supplied context tells the model to give a short `Memory R<number>: useful/noise/uncertain` rating.
At 50 recorded nonempty blocks, the hook gives you a prompt to paste into Claude or Codex for a source-checked batch inspection.
The reminder continues until acknowledgment of that complete batch.
See [automatic recall inspection](REFERENCE.md#reviewing-automatic-recall).
Model ratings alone do not prove that answers improved.

## Read knowledge and request bounded learning

A related memory question can make your selection necessary.
The agent prepares and shows the complete proposed change in conversation.
That display includes the specified approval and reject reply sentences for its proposal ID.
Send the selected sentence as a full plain chat message.
The agent checks your initial reply and handles all commands.

A proposal view gives no approval.
A reply alone does not prove stored memory.
For an edit, the agent prepares a new complete proposal with a new display and new approval.
The agent reports stored memory only after Muninn gives the actual stored receipt.
A checked decision can stay pending or blocked.
Ordinary learning with checked source records keeps its automatic storage without a new approval step.

These commands are agent operations:

```sh
muninn know unresolved
muninn know decision stage --input /path/to/candidate.json
muninn know decision show D_ID
muninn know decision submit --input /path/to/intent.json
muninn know decision status D_ID --human
muninn know decision apply D_ID
```

`D_ID` identifies one complete proposal.
Use the actual ID from the `stage` command.
The agent sends the unmodified decoded show `text` as its full last message.
It adds no prose, Markdown fence, or newline external to that text.
The stored policy limits the complete display and its reply instructions.
Muninn refuses a display that cannot fit.

It does not show a shortened version for approval.
See [the contract](REFERENCE.md#conversational-memory-decisions) for the checked input and output fields.

You can also examine current knowledge with source checks:

```sh
muninn know list
muninn know search "project convention"
muninn know context
muninn learn list
```

Current knowledge uses `K_` UUID IDs and explicit revisions.
Context contains current applicable notes with source proof and a guide index.
It does not include withheld, expired, superseded, retracted, erased or unsupported guidance.
Open guides with `muninn know show K_ID`, then open cited source records before using them.

```sh
muninn learn on
muninn learn show R_ID
muninn learn retry R_ID
muninn know propose --input /path/to/proposal.json
muninn know retract K_ID --expected-revision 1 --reason "Withdrawn by owner"
```

Replace placeholders with actual indexed task and knowledge IDs and explicit files.

Retry explicitly starts the same task again when permitted.
Completed receipts remain idempotent.
Proposals contain `request_id` plus strict `result`, use admitted source proof and pass checked commit.
They invoke no model and cannot invent owner authority.
Corrections use proposed replacements with source checks.
See [the full contracts](REFERENCE.md#current-knowledge-and-learning).

## Verify the installation

```sh
muninn doctor            # exit 0 = healthy
muninn --pretty stats    # counts. Add --pretty for indented JSON
muninn search "a phrase you remember"
```

## Cursor history

On a new installation, Muninn checks for Cursor's database at `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`.
It imports the database when available.
Cursor installation is not necessary.
The installer prints which of Claude, Codex and Cursor have history to index.
`--dry-run` reports the proposed index work without importing anything.

The installer can merge the native Cursor hook into `~/.cursor/hooks.json`.
For manual setup, copy managed lifecycle handlers from the installed release's `integrations/cursor/hooks.json`.
Keep other hooks in `~/.cursor/hooks.json`.
The installed fragment contains the escaped native launcher command.
It is at `~/.local/lib/muninn/current/`.

Before compaction, the hook selects one `composerData:<conversation_id>` equal to the documented hook identity.
It reads at most the `message_count` bubbles reported by Cursor.
It checks one more bubble to find an oversized conversation.
If a stored `composerId` exists, it must agree.

Missing, malformed, duplicate or unequal identity skips before writes.
There is no newest-row fallback.
Scope uses the hook's correct native absolute `workspace_roots`.
A single root is authoritative.
With multiple roots, a payload `cwd` equal to a listed root or in its subtree has precedence before inherited `CURSOR_PROJECT_DIR`.
The environment directory is a fallback only when it also agrees with a listed root.

Missing, incorrect or ambiguous roots skip before writes.
If the stored conversation has more bubbles, Muninn skips the full conversation.
`muninn ingest --full` remains the complete import path.
The hook does not add context to the compacted turn.
Cursor has individual native `sessionStart`, `sessionEnd` and `preCompact` handlers.

Startup refresh binds the native workspace independently of history capture.
sessionEnd imports the specified ended conversation without `message_count` as a prerequisite.
End/precompact enqueue bounded learning windows only while learning is on (`muninn learn on`).
Busy or unavailable capture gets no acknowledgment of durable storage.
Recovery cannot prove the specified missed notification or cutoff.

Context from hooks after a resumed session or compaction remains unproven.
Explicitly run `muninn know context` after those operations.
Context that a hook supplied cannot be retracted.
Prompt recall continues to follow `recall.off`.

A refresh of the specified conversation also corrects stored record scope for unchanged content previously imported with a different cwd.
Ordinary full imports keep their existing behavior.

See [Cursor's hook contract](https://cursor.com/docs/hooks) for the hook fields.
Their mapping to the local database is empirical.
A [Cursor bundle analysis](https://forum.cursor.com/t/cursor-hooks-intermittently-emit-empty-conversation-id-session-id-on-tool-execution-events/167095) connects lifecycle hook `conversation_id` to `composerId`.
[Exporter source](https://github.com/Callum-Ward/cursaves/blob/main/cursor_saves/export.py#L108) looks up `composerData:<composerId>`.
The database format is not a vendor-supported storage API.
A format change may cause a skipped refresh.

Actual Cursor lifecycle execution on this checkout remains unverified.

```sh
muninn search "a phrase from Cursor" --provider cursor
```

## Installed files

| Path | What |
|---|---|
| `~/.local/lib/muninn/<sha>/` | the one pinned release |
| `~/.local/lib/muninn/current`, `python` | symbolic links to it and the interpreter |
| `~/.local/lib/muninn/install-record.json` | sha, Python, changed config keys, outcome |
| `~/.local/lib/muninn/install.log` | timestamped steps and exit codes, no transcript text |
| `~/.local/share/muninn/` | the database, logs and heartbeat |
| `~/.local/bin/muninn` | symbolic link to `current/bin/muninn` |

A new project can initially have no indexed project memory.
This is usual empty context, not proof of database damage.
Indexing makes the project mapping when it indexes a record.
SessionEnd seeks learning from records in the index.
It does not run ingest or wait.

A subsequent startup, session continuation, clear, or compact can read the new mapping.
Prompt recall operates independently.
Waiting alone does not refresh the current conversation's startup context.
