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

`bin/muninn-install` installs this checkout's `HEAD`. It runs a fresh install when
neither the data directory nor release exists, and an upgrade when both exist. An
incomplete installation (only one exists) is refused. `--status` compares the installed
commit with `HEAD`. Other arguments are forwarded to `install.installer` (`--repo`,
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
above. Historical unmarked stores require an explicit cutover; ordinary writers
and rebuild refuse them. Legacy activation/restart is not implemented by the
current-format maintenance route.

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

### Explicit first review policy

On macOS/Linux, an explicitly supplied validated policy can enable first review
admission after store preparation and provider configuration:

    bin/muninn-install --repo /path/to/muninn --sha FULL_40_HEX_SHA --fresh --review-policy /path/to/policy.json

Use `--upgrade` for the exact legacy route or a marked store that has never been
activated. These are the existing two install modes. There is no policy discovery
or default policy. Without this modifier, a never-activated store completes with
`policy_status: activation_pending` and review admission remains disabled.
A configured current store preserves its existing admission and reports
`policy_status: configured`.
An identical installed policy preserves its admission and source boundaries;
a different policy is refused. Disabled configured admission is never reenabled.

The private operation record freezes input bytes and canonical policy. On a
restart use the same checkout and operation; omitting the policy path uses the
frozen snapshot, while supplying changed input refuses. Actual production policy,
budget approval and native runner capability still require separate evidence;
a synthetic policy or successful installation does not establish them.

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

To turn recall on: `unlink ~/.local/share/muninn/recall.off`. To turn it off again:
`install -m 600 /dev/null ~/.local/share/muninn/recall.off`. Details in
`docs/REFERENCE.md`. An `--upgrade` never changes the file.

With recall enabled, supplied context asks the model for a short `Memory R<number>:
useful/noise/uncertain` assessment. At 50 recorded nonempty blocks, the hook gives you
a prompt to paste into Claude or Codex for a source-backed batch review. The reminder
continues until that complete batch is acknowledged. See
[reviewing automatic recall](REFERENCE.md#reviewing-automatic-recall); model ratings
alone don't prove that answers improved.

## Read knowledge and request bounded learning

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
