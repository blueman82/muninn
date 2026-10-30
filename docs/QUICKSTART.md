# Quick start

## Prerequisites

- macOS (launchd), Linux (systemd user service) or Windows x64/ARM64 (Task Scheduler).
- Python 3.13 or newer. The installer pins the interpreter it runs under; set
  `MUNINN_PYTHON=/path/to/python3.13` to override at run time.
- `git`, and a clean clone of this repo checked out at the commit to install.
- Optional desktop apps: Claude, Codex, Cursor.
- Absent app: hook setup skipped.
- Installed Claude/Codex without `~/.claude/settings.json`/`~/.codex/config.toml`:
  manual setup guidance.

## Install on a new machine

From the repository root, with a clean worktree at the required commit:

    bin/muninn-install --check    # plain-sentence preview; writes nothing
    bin/muninn-install

`--check` never creates an environment, installs packages or enables Git hooks.
It reuses a compatible selected interpreter or this checkout's ready `.venv`.
If the pinned runtime is missing, prepare it separately before retrying:

    python3.13 -m venv .venv
    .venv/bin/python3.13 -m pip install --require-hashes -r requirements-runtime.lock

Alternatively, set `MUNINN_PYTHON` to an existing compatible environment. A normal
install can still offer runtime and Git-hook setup; accepting those prompts changes
the checkout. That setup is separate from the read-only preview.

An upgrade preview also needs the existing private writer lock and a stable
DELETE-mode store without journal, WAL or shared-memory sidecars. It refuses
missing, busy, unsafe or changing state with `review_policy_preview_unavailable`.
The preview never creates a lock, repairs permissions, stops the service or
clears recovery state. Preserve refused state and use the existing recovery
guidance; the preview does not make it safe to install.

`bin/muninn-install` installs this checkout's `HEAD`. It runs a fresh install when
neither the data directory nor release exists, and an upgrade when both exist. An
incomplete installation (only one exists) is refused. `--status` compares the installed
commit with `HEAD` using local Git history. It reports whether the checkout or
installed version is newer. If the installed version is newer, update the checkout
before installing. Unknown or divergent history reports `versions differ`; inspect
the revisions before installing. Status does not fetch from the network or change
the installation. Other arguments are forwarded to `install.installer` (`--repo`,
`--sha`, `--fresh`, `--upgrade`, `--dry-run`, `--home`, `--review-policy`), which needs a full 40-hex
`--sha` equal to `HEAD`. A fresh install refuses if the data directory or the launchd
plist already exists. It pins the release, indexes your existing transcripts, configures the managed provider hooks, starts the
poller, runs
`muninn doctor`, and prunes to one release. Before live-store mutation, failures can
roll back installer changes. After possible mutation, current-format upgrade keeps
current data and the selected runtime/configuration, leaves the service stopped,
and records `recovery_required`. Retained archives are not automatic restore
authority; see [the recovery contract](REFERENCE.md#current-knowledge-and-learning).

| Platform | Installed desktop app evidence |
|---|---|
| macOS | `Claude.app`, `Codex.app`, `Cursor.app` in `/Applications` or `~/Applications` |
| Windows | Desktop executables in LocalAppData/Program Files; current-user `Claude`/`OpenAI.Codex` MSIX registration with an existing executable declared in its manifest |
| Linux | `claude-desktop`, `chatgpt`, `cursor` on PATH |

- Provider setup/config validation/hook verification: installed app required.
- Leftover settings/transcripts: never installation evidence.
- Absent app: setup skipped.
- Installed Claude/Codex without config: manual setup guidance.
- History import/existing index: independent of app detection.
- Cursor setup prompt: installed Cursor; interactive install only.

An interactive install asks whether to configure Muninn's Cursor
`sessionStart`, `sessionEnd` and `preCompact` hooks automatically or leave them
for manual setup. Automatic setup merges only Muninn's handlers into `~/.cursor/hooks.json`, preserving other
hooks. Rollback restores prior owned handlers while retaining current foreign
additions, edits, deletions and order. On a non-interactive install, or if you
choose manual, no Cursor config is changed; the installer prints the target path and a direct link to the
instructions below. An upgrade only changes Cursor settings if you choose
automatic setup.

Codex only runs plugin hooks after you trust them: start a Codex session and run
`/hooks` if the installer prints `OWNER STEP`.

## Upgrade an installed machine

    bin/muninn-install --check
    bin/muninn-install

The installer selects the commit, restarts the poller, verifies, then deletes every
other release. It keeps the existing index instead of running the fresh-install history
import; after a successful upgrade, Claude and Codex polling resumes, while Cursor
history is not re-imported. Fresh and upgrade reconcile managed Claude/Codex definitions while preserving
unrelated hooks; Cursor reconciliation follows the automatic setup choice. Changed
definitions require owner approval; use `/hooks` for Codex when prompted. When a release changes how
transcripts are classified (`stats` shows `classifier_version`), the poller re-reads
every transcript once after the upgrade, Claude and Codex alike, one source at a time;
large stores may require additional time. Monitor progress with `muninn stats`
(`reread.pending` falls towards 0) or `muninn doctor` (the `reread` line). The re-read
holds the writer lock longer, so other writers see exit 3 meanwhile, and it can leave
free pages: if `doctor` then warns `db_free_space`, run `muninn compact`. A failed
upgrade may require recovery instead of returning to the old release, as described
above. Ordinary writers and rebuild refuse unmarked stores. During `--upgrade`, a
recognized schema v3 store is copied
into the marked format after the managed poller stops. The installer keeps a private
copy of the entire old database, including its old knowledge ledger; only indexed
history is copied into the new store, and the latest erasure is applied before
publication. Other unmarked schemas remain untouched and refuse upgrade.

The exact legacy release `5c1a69b0edff81df80836d72a80971a91a8f9520`
has a macOS/Linux continuation after managed launch exclusion and a later OS boot.
All old manual commands, source/module invocations, imported clients and custom
jobs must remain stopped through upgrade and recovery. Repeat the same prepared
checkout and `--upgrade --repo DIR --sha SHA` after the restart. The installer
retains the original archive, copies indexed history, applies latest erasure,
and publishes the marked store before selecting compatible configuration.
Preserve its private operation directory and launch exclusion if interrupted;
recovery proceeds forward from verified actual state. Windows legacy closure and
live native qualification remain separate prerequisites. See [REFERENCE.md](REFERENCE.md).

### Default learning policy

A learning policy tells Muninn when it may send captured conversation text to
the learning runner. It also sets limits for each request. Every clone includes
the default profile at `tools/default_codex_policy.json`.

Muninn applies the profile during an install that uses the current default home
when the store has no learning policy. The host must meet the Codex runner
requirements. It must use Apple silicon macOS. A signed Codex CLI must be on
`PATH`. The Codex sign-in directory must exist. Muninn records the host, data
directory and executable in a private capability file. If a check fails,
installation can finish, but learning stays pending. Trust Codex hooks when
Codex prompts you at `/hooks`.

The recall modes `recall.off`, `recall.advisory` and `recall.on` control prompt
recall. They do not control learning from completed sessions.

During first setup, the installer asks which records to review:

- **From now:** review records created after this install or upgrade.
- **All available history:** review verified older project records, then review new records.

The installer keeps your choice for later upgrades.
An ordinary upgrade with no new learning or policy request preserves the saved
scope and its original boundary. It does not ask to review additional history
or queue that history to complete the upgrade. An explicit scope or policy
request still uses the normal review and consent checks.
If you choose history, it shows eligible requests, excluded ranges, and a time estimate before confirmation.
A capture gap does not equal a review request.
Muninn excludes missing, erased, changed, or unverifiable source ranges.
For late imports, From now also excludes older or undated records.

To select future records without a scope prompt, run:

```sh
bin/muninn-install --learning-scope now
```

Each attempt allows two calls, 1 MiB of input, 32 KiB of output, 12,000 context bytes, and 120 seconds.
Revision 3 allows four concurrent requests and three total attempts per request.
Temporary failures wait 5 seconds, then 30 seconds, before their remaining attempts.
Manual and live requests take priority over queued history.
Within each group, Muninn processes older requests first.
Muninn checks the current knowledge revision before saving each result.

The policy allows project scope only.
It has no total call limit or expiry.
These controls cannot limit subscription allowance or predict subscription cost.
An API cost estimate requires comparable calls with reported costs.
A small sample gives a low-confidence estimate.
Time estimates use whole worker batches.
The installer separates first-attempt estimates from estimates with permitted retries.
Estimates exclude setup waits and database delays.

The running background program can recheck unavailable setup while learning is enabled.
It waits at least 60 seconds. Reading a warning starts no timer.
One rejected request does not stop unrelated requests under revision 3.
If help is required, the next Codex session shows the cause and action.
You do not need to run `doctor` to receive this message.
Muninn preserves owner pauses and blocks calls whose stopped state remains unverified.

To customise the profile, edit `tools/default_codex_policy.json` in your private clone.
Commit the change before installation.
For revision 3, set `concurrency`, `max_attempts`, and `retry_backoff_seconds` under `revisions.3`.
Use one backoff value for each permitted retry.
Keep concurrency at or below 4 and attempts at or below 3.
The `limits` object controls the other byte, round, context, and deadline limits.
You can reduce these limits within the enforced bounds.
Keep the runner, model, reasoning, host binding, and project-scope settings.
Record the approver in `approved_by` and the UTC approval time in `approved_at`.

Eligible repository defaults can move from revision 1 or 2 to revision 3 during upgrade.
The new approval time must be later.
Muninn preserves old request policies and knowledge.
It marks unstarted old-policy requests as replaced.
The selected learning scope controls new admission.
Changed custom policies retain their existing limits.
If learning is paused, the installer asks separately before enabling it.
Declining keeps the pause.
An accepted restart requires every recorded call and attempt to have finished.
An unchanged legacy default can receive this checked policy update.
The update retains its recorded runner version and host settings.
For an unattended, explicitly approved restart, use `bin/muninn-install --enable-learning`.
This flag does not bypass process checks or approve changed custom policies.
A retained From now choice keeps its original start time.
The installer confirms replacement work before replacing an old queue.
Do not edit the store by hand.
See the [policy reference](REFERENCE.md#default-learning-policy).

## Uninstall

    bin/muninn-uninstall --dry-run    # says what it would do; writes nothing
    bin/muninn-uninstall

It stops the poller, removes the launchd plist, removes only Muninn's hooks from
`~/.claude/settings.json`, Muninn's managed lifecycle handlers from
`~/.cursor/hooks.json`, and only Muninn's sections from `~/.codex/config.toml`,
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

Advisory mode gives the model a short `<muninn-retrieval>` instruction to search
project history when it matters, without automatic recall or a hook store read.
Create `~/.local/share/muninn/recall.advisory` with mode 0600 and remove
`recall.off` to select it. With both files present, off wins.

To turn recall on: `unlink ~/.local/share/muninn/recall.off`. To turn it off again:
`install -m 600 /dev/null ~/.local/share/muninn/recall.off`. Details in
`docs/REFERENCE.md`. An `--upgrade` never changes either mode file.

With recall enabled, supplied context asks the model for a short `Memory R<number>:
useful/noise/uncertain` assessment. At 50 recorded nonempty blocks, the hook gives you
a prompt to paste into Claude or Codex for a source-backed batch review. The reminder
continues until that complete batch is acknowledged. See
[reviewing automatic recall](REFERENCE.md#reviewing-automatic-recall); model ratings
alone don't prove that answers improved.

## Read knowledge and request bounded learning

A relevant memory question can need your choice.
The agent prepares and shows the complete proposed change in conversation.
That display includes exact approve and reject reply sentences for its proposal ID.
Send the chosen sentence as a whole plain chat message.
The agent verifies your original reply and handles all commands.

Viewing a proposal gives no approval. A reply alone does not establish saved memory.
For an edit, the agent prepares a new complete proposal with a new display and fresh approval.
The agent reports saved only after Muninn returns the actual saved receipt.
A checked decision can remain pending or blocked.
Ordinary supported learning still saves without a new approval step.

These commands are agent operations:

    muninn know unresolved
    muninn know decision stage --input /path/to/candidate.json
    muninn know decision show D_ID
    muninn know decision submit --input /path/to/intent.json
    muninn know decision status D_ID --human
    muninn know decision apply D_ID

`D_ID` identifies one complete proposal. Use the real ID returned by stage.
The agent sends the exact decoded show `text` as its whole final message.
It adds no prose, Markdown fence, or newline outside that text.
The saved policy limits the complete display, including its reply instructions.
Muninn refuses a display that cannot fit. It does not show a shortened version for approval.
See [the reference](REFERENCE.md#conversational-memory-decisions) for the checked input and output fields.

Current supported knowledge can also be inspected:

    muninn know list
    muninn know search "project convention"
    muninn know context
    muninn learn list

Current knowledge uses `K_` UUID IDs and explicit revisions. Context contains
current applicable supported notes and a guide index, excluding withheld,
expired, superseded, retracted, erased or unsupported guidance. Open guides with
`muninn know show K_ID`, then open cited originals before relying on them.

    muninn learn request --session codex:THREAD --from-line 2 --through-line 8 --policy /path/to/policy.json
    muninn learn show R_ID
    muninn learn retry R_ID
    muninn learn replay --session codex:THREAD --from-line 2 --through-line 8 --policy /path/to/policy.json
    muninn know propose --input /path/to/proposal.json
    muninn know retract K_ID --expected-revision 1 --reason "Withdrawn by owner"

Replace placeholders with actual indexed session/request/knowledge IDs and explicit
files. Requests require the installed admitted immutable policy; `--policy` must
match it. There are no implicit runner/model/budget settings. Request bounds must
be paired; replay requires bounds and policy. Retry explicitly restarts the same
request when still permitted; completed receipts remain idempotent. Proposals
contain `request_id` plus strict `result`, use admitted evidence and pass checked
commit; they invoke no model and cannot invent owner authority. Corrections use
supported proposed replacement. `know add`, `know check` and their old flags are
retired. See [the exact contracts](REFERENCE.md#current-knowledge-and-learning).

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

The installer can merge the native Cursor hook into `~/.cursor/hooks.json`.
For manual setup after installation, copy the managed lifecycle handlers from the
installed release's `integrations/cursor/hooks.json`, preserving other hooks in
`~/.cursor/hooks.json`. The installed fragment already contains the escaped native
launcher command. On macOS/Linux it is under `~/.local/lib/muninn/current/`; on
Windows it is under the selected `%LOCALAPPDATA%\Muninn\lib\<sha>\` release.
Outside macOS, set `MUNINN_CURSOR_DB` to Cursor's native absolute database path in
Cursor's environment; without it, the hook skips the import. Before compaction,
the hook selects exactly one `composerData:<conversation_id>` matching the
documented hook identity and processes at most the `message_count` bubbles
reported by Cursor, probing one additional bubble to detect an oversized
conversation. A present stored `composerId` must also match. Missing, malformed,
duplicate or mismatched identity skips before writes; there is no newest-row
fallback. Scope uses the hook's valid native absolute `workspace_roots`: a
single root is authoritative. With multiple roots, a payload `cwd` equal to or
inside a listed root takes precedence over inherited `CURSOR_PROJECT_DIR`;
the environment directory is a fallback only when it also matches a listed root.
Missing, invalid or ambiguous roots skip
before writes. If the stored conversation has more bubbles, Muninn skips
that conversation whole; `muninn ingest --full` remains the complete import
path. The hook does not add context to the compacted turn.
Cursor has distinct native `sessionStart`, `sessionEnd` and `preCompact` handlers.
Startup refresh binds the native workspace independently of history capture;
sessionEnd imports the exact ended conversation without requiring `message_count`.
End/precompact enqueue bounded learning windows only with an admitted frozen policy.
A busy/unavailable capture is not acknowledged as durable; recovery cannot prove
the exact missed event/cutoff. Resume/postcompact injection remains unproven:
explicitly run `muninn know context` after resume or compaction. Already injected
context cannot be retracted. Prompt recall still follows `recall.off`.
An exact refresh also corrects stored event scope when unchanged content was previously imported under another cwd;
ordinary full imports keep their existing behavior.

The hook fields are documented in [Cursor's hook contract](https://cursor.com/docs/hooks).
Their mapping to the local database is empirical: a [Cursor bundle analysis](https://forum.cursor.com/t/cursor-hooks-intermittently-emit-empty-conversation-id-session-id-on-tool-execution-events/167095)
links lifecycle hook `conversation_id` to `composerId`, and [exporter source](https://github.com/Callum-Ward/cursaves/blob/main/cursor_saves/export.py#L108)
looks up `composerData:<composerId>`. The database format is not a vendor-supported
storage API; a format change may cause a skipped refresh. Real Cursor lifecycle
execution on this checkout remains unverified.

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

A new project can initially have no indexed project memory.
This is normal empty context, not proof of database damage.
Indexing creates the project mapping when it indexes an event.
SessionEnd requests learning from already indexed records. It does not ingest or wait.
A later startup, resume, clear, or compact can read the new mapping.
Prompt recall remains separate. Waiting alone does not refresh the current conversation's startup context.

If learning reports `codex_host_changed`, ask the agent to check store-format and
recovery prerequisites, then prepare the non-writing host-renewal preview in
[REFERENCE.md](REFERENCE.md#renew-an-approved-codex-computer-binding). An installed
format-1 store needs an ordinary upgrade to format 2 first. That step grants no
new host permission. The later renewal needs your affirmative answer for its
exact displayed digest. A full upgrade can run Codex hook checks and start
background learning before returning; ongoing approval has no total call or
spending cap of its own.
