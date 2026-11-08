# Quick start

## Prerequisites

- macOS (launchd), Linux (systemd user service) or Windows x64/ARM64 (Task Scheduler).
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

An upgrade preview must have the existing private writer lock and a stable DELETE-mode store without journal, WAL or shared-memory sidecars.
It refuses missing, busy, dangerous or changing conditions with `review_policy_preview_unavailable`.
The preview does not make a lock, repair permissions, stop the service or clear recovery conditions.
Keep refused conditions and use the existing recovery guidance.
The preview does not make it safe to install.

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

Other arguments go to `install.installer`: `--repo`, `--sha`, `--fresh`, `--upgrade`, `--dry-run`, `--home`, and `--review-policy`.
The installer must have a full 40-hex `--sha` equal to `HEAD`.
A new installation refuses an existing data directory or launchd plist.
It pins the release, indexes existing transcripts, configures managed provider hooks, and starts the poller.
It then runs `muninn doctor` and prunes to one release.

Before live-store mutation, errors can cause rollback of installer changes.
After possible mutation, an upgrade of the current format keeps current data and the selected runtime/configuration.
It keeps the service stopped and records `recovery_required`.
Archives that remain do not give automatic authority to replace current data.
See [the recovery contract](REFERENCE.md#current-knowledge-and-learning).

| Platform | Installed desktop app proof |
|---|---|
| macOS | `Claude.app`, `Codex.app`, `Cursor.app` in `/Applications` or `~/Applications` |
| Windows | Desktop executables in `LocalAppData`/`Program Files`. Current-user `Claude`/`OpenAI.Codex` MSIX registration with an existing executable declared in its manifest |
| Linux | `claude-desktop`, `chatgpt`, `cursor` on PATH |

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

Ordinary writers and rebuild refuse unmarked stores.
During `--upgrade`, the installer stops the managed poller, then copies a recognized schema v3 store into the marked format.
It keeps a private copy of the full previous database and its previous knowledge ledger.
It copies only indexed history into the new store and applies the latest erasure before publication.
It does not change other unmarked schemas and refuses their upgrade.

The specified legacy release `5c1a69b0edff81df80836d72a80971a91a8f9520` has a macOS/Linux continuation after managed launch exclusion and a subsequent OS boot.
Keep all previous manual commands, source/module invocations, imported clients and custom work stopped through upgrade and recovery.
After the computer starts again, repeat the same prepared checkout and `--upgrade --repo DIR --sha SHA`.
The installer keeps the initial archive, copies indexed history, and applies the latest erasure.
It publishes the marked store before selecting compatible configuration.

If interrupted, keep its private operation directory and launch exclusion.
Recovery proceeds forward from checked actual conditions.
Windows legacy closure and live native qualification remain prerequisites that apply independently.
See [`REFERENCE.md`](REFERENCE.md).

### Default learning policy

A learning policy tells Muninn when it can send captured conversation text to the learning runner.
It also sets limits for each learning task.
Each clone includes the default policy template at `tools/default_codex_policy.json`.

Muninn applies the policy template during an install that uses the current default home when the store has no learning policy.
The host must satisfy the Codex runner requirements: Apple silicon macOS, a signed Codex CLI on `PATH`, and an existing Codex sign-in directory.
Muninn records the host, data directory and executable in a private runner-check file.
If a check does not succeed, installation can finish, but learning stays pending.
Trust Codex hooks when Codex prompts you at `/hooks`.

The recall modes `recall.off`, `recall.advisory` and `recall.on` control prompt recall.
They do not control learning from completed sessions.

During first setup, the installer gives two selections for the records to examine:

- **`From now`:** examine records made after this install or upgrade.
- **All available history:** examine checked older project records, then new records.

The installer keeps your selection for subsequent upgrades.
An ordinary upgrade with no new learning or policy instruction keeps the stored scope and its initial boundary.
It does not prompt for approval to examine more history or queue that history to complete the upgrade.
An explicit scope or policy instruction uses the usual inspection and consent checks.
For history, the installer shows eligible learning tasks, excluded ranges, and a time estimate before confirmation.

A capture omission does not equal a learning task.
Muninn excludes missing, erased, changed, or unverifiable source ranges.
For late imports, `From now` also does not include older or undated records.

To select future records without a scope prompt, run:

```sh
bin/muninn-install --learning-scope now
```

Each try allows two calls, 1 MiB of input, 32 KiB of output, 12,000 context bytes, and 120 seconds.
Revision 3 allows four concurrent learning tasks and three total tries for each task.
Temporary errors wait 5 seconds, then 30 seconds, before their remaining tries.
Muninn handles manual and live work before queued history.
In each group, Muninn handles older work first.
Muninn checks the current knowledge revision before keeping each result.

The policy allows project scope only.
It has no total call limit or expiry.
These controls cannot limit subscription allowance or predict subscription cost.
An API cost estimate must have comparable calls with reported costs.
A small sample gives a low-confidence estimate.

Time estimates use full worker batches.
The installer gives first-try estimates and estimates with permitted retries independently.
Estimates exclude setup waits and database wait times.

The active background software can check unavailable setup again while learning stays enabled.
It waits a minimum of 60 seconds.
A warning does not start a timer when you read it.
One rejected task does not stop unrelated tasks with revision 3.
If help is necessary, the next Codex session shows the cause and necessary step.

You do not have to run `doctor` to receive this message.
Muninn keeps owner pauses and blocks calls without proof that they stopped.

To customise the policy template, edit `tools/default_codex_policy.json` in your private clone.
Commit the change before installation.
For revision 3, set `concurrency`, `max_attempts`, and `retry_backoff_seconds` in `revisions.3`.
Use one backoff value for each permitted retry.
Keep concurrency at or below 4 and tries at or below 3.

The `limits` object controls the other byte, call, context, and deadline limits.
You can decrease these limits in the enforced limits.
Keep the runner, model, `reasoning`, host binding, and project-scope settings.
Record the approver in `approved_by` and the UTC approval time in `approved_at`.

Eligible repository defaults can move from revision 1 or 2 to revision 3 during upgrade.
The new approval time must follow the previous time.
Muninn keeps previous task policies and knowledge.
It marks unstarted previous-policy tasks as replaced.
The selected learning scope controls new admission.
Changed custom policies keep their existing limits.

With learning paused, the installer prompts for approval independently before enabling it.
Declining keeps the pause.
An accepted start must have all recorded calls and tries finished.
An unchanged legacy default can get this checked policy update.
The update keeps its recorded runner version and host settings.

For an unattended, explicitly approved start, use `bin/muninn-install --enable-learning`.
This flag does not bypass OS task checks or give approval to changed custom policies.
A `From now` selection that remains keeps its initial start time.
The installer confirms replacement work before replacing a previous queue.
Do not edit the store by hand.
See the [policy contract](REFERENCE.md#default-learning-policy).

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
An `--upgrade` does not change the two mode files.

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
muninn learn request --session codex:THREAD --from-line 2 --through-line 8 --policy /path/to/policy.json
muninn learn show R_ID
muninn learn retry R_ID
muninn learn replay --session codex:THREAD --from-line 2 --through-line 8 --policy /path/to/policy.json
muninn know propose --input /path/to/proposal.json
muninn know retract K_ID --expected-revision 1 --reason "Withdrawn by owner"
```

Replace placeholders with actual indexed session, task and knowledge IDs and explicit files.
Learning tasks must use the installed admitted immutable policy.
`--policy` must equal it.
There are no implicit runner, model or budget settings.
Task line limits must be paired.
Replay must have line limits and policy.

Retry explicitly starts the same task again when permitted.
Completed receipts remain idempotent.
Proposals contain `request_id` plus strict `result`, use admitted source proof and pass checked commit.
They invoke no model and cannot invent owner authority.
Corrections use proposed replacements with source checks.
Do not use `know add`, `know check` or their previous flags.

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
On macOS/Linux, it is at `~/.local/lib/muninn/current/`.
On Windows, it is at the selected `%LOCALAPPDATA%\Muninn\lib\<sha>\` release.

On systems other than macOS, set `MUNINN_CURSOR_DB` to Cursor's native absolute database path in Cursor's environment.
Without it, the hook skips the import.
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
End/precompact enqueue bounded learning windows only with an admitted frozen policy.
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

If learning reports `codex_host_changed`, tell the agent to check store-format and recovery prerequisites.
Then prepare the preview that writes nothing in [`REFERENCE.md`](REFERENCE.md#renew-an-approved-codex-computer-binding).
An installed format-1 store must have an ordinary upgrade to format 2 first.
That step grants no new host permission.
The subsequent renewal must have your affirmative answer for its specified displayed digest.

A full upgrade can run Codex hook checks and start background learning before the command ends.
Ongoing approval has no total call or spending cap of its own.

### Keep learning on the current managed Codex

A stored executable path can become unavailable after Codex updates.
On Apple silicon macOS, prepare the explicit stable interface rule with:

```sh
bin/muninn-install --upgrade --codex-interface preview --check
```

The preview writes nothing and makes no model call.
It can check the stored approval when the previous executable is unavailable.
Apply its specified digest with `bin/muninn-install --upgrade --codex-interface PLAN_DIGEST`.
The installer seeks a new `yes` or `y` for that rule.
It keeps account, model, scope, history, limits and existing work.
Compatible subsequent Codex updates have no new approval prerequisite.

This trusts the fixed local manager, not an arbitrary executable on `PATH`.
See [the full contract](REFERENCE.md#follow-the-current-trusted-codex-installation).
