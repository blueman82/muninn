# Troubleshooting

Run `muninn --pretty doctor`.
Each check has `ok`, `level`, and `detail`.
An error-level check with `ok: false` makes the exit code 1.
`warn` and `info` do not make the run unsuccessful.
When asking for help, share `doctor`, `stats`, `install.log` and `install-record.json`.

None of them holds transcript text.

| Symptom | Likely cause | What to do |
|---|---|---|
| `muninn: no Python 3.13+ found` | No interpreter on PATH or at the installer's link | Set `MUNINN_PYTHON=/path/to/python3.13`, or re-run the installer with the right Python |
| `poller: stale` in each answer, `heartbeat` fails | The launchd service stopped or cannot continue. | Run `launchctl print gui/$(id -u)/com.muninn`. Read `poller.log`. Run `launchctl kickstart -k gui/$(id -u)/com.muninn`. |
| `poller: stale` while the poller runs | One source takes more than 3 intervals. Muninn writes the alive timestamp between sources, not during one source. The poller could use a release from before the alive-timestamp feature. | Wait for the pass to end. Check `reread.pending` and `last_pass.alive_age_s` in `muninn stats`. A continuously increasing `alive_age_s` indicates a pass that stopped without a new poller start. `poller.log` gets a line only after a pass finishes. A pass with recurring errors or writer contention writes no heartbeat and reports `stale`. |
| `launchd_job` fails | The service has no launchd registration. | Run `bin/muninn-install` again. It upgrades an installed machine. |
| `muninn search` finds small numbers of results | Small numbers of indexed sources. | Run `muninn --pretty stats`. Check `sources`, `events_by_provider`, `last_pass.unreadable_files`, and `last_pass.skipped_files`. A provider count of 0 indicates that Muninn found no root for it. `last_pass.unreadable_files` counts files the poller could not open. `muninn doctor` shows `roots_present`. |
| `db_free_space` warns | Many deletes left free pages | `muninn compact` (needs about one database's worth of free disk) |
| `aside_files` warns | Historical unreadable files or kept rebuild archives include copies that did not finish. Erasure cannot scrub them. | Examine the reported paths. Keep recovery `evidence` until the owner explicitly approves removal. `aside_remove` supplies literal removal commands. It does not automatically put data back. |
| `upgrade_snapshot` warns | A governed pre-upgrade archive stays and can contain erased text | Keep it while recovery is unresolved. Only remove the named archive after an explicit owner decision, using the literal `aside_remove` command |
| Exit 4, `error` `tombstone_key`. `doctor` `tombstone_key` fails (`missing` or `damaged`) | Keyed tombstones exist but `tombstone.key` is not on disk or has damage. Index, rebuild, and erase refuse to run. | Put `tombstone.key` back from a recovery copy. Do not make a new key. A new key prevents the current tombstones from matching, so erased content could come back. |
| `release_leftovers` warns | An upgrade sweep could not remove a previous `.pruning-<ts>-<sha>` release directory in `~/.local/lib/muninn`. | The next `bin/muninn-install` retries the sweep. You can also remove the named directory manually. |
| `db_size` warns | The database exceeds 2 GiB. | Run `muninn compact`. You can use `muninn erase` for previous sessions. |
| Exit 3, `busy` | A different writer holds the lock (poller pass, compact, erase) | Retry in a small number of seconds |
| `store_unavailable` (hooks say `memory unavailable`) | The database cannot be read, or its schema is not one that this release accepts. | Keep the file. Ordinary index and rebuild operations do not change a schema. Run `bin/muninn-install` for an accepted older store. |

| Symptom | Likely cause | What to do |
|---|---|---|
| Exit 4, `hot_journal` / `store_unavailable` | A writer crashed during a transaction, or the schema differs. | Run a writing command, such as `muninn ingest`, to roll the journal back. `doctor` shows `unowned_journal`. |
| `unexpected_files` fails | A stray file exists in the data directory. | Move it out. `recall.off`, `recall.advisory`, and logs are permitted files. |
| Hooks print nothing | `MUNINN_HOOK_DISABLE=1`, `recall.off`, or an empty/slash prompt | Unset the disable flag or select `recall.advisory` for model-led search instructions |
| Codex shows no memory | Plugin hooks not trusted | Start Codex, run `/hooks`, trust the two hooks |
| Cursor history has no records after compaction | The optional native hook, database, payload, workspace, or refresh checks could not complete. | Use the [Cursor history checks](#cursor-history-checks) below. |
| `app not installed: setup skipped`. No Cursor prompt | No desktop app exists. Remaining configurations and transcripts do not prove installation. | See [App detection](QUICKSTART.md#install-on-a-new-machine). Installed Claude/Codex without configuration needs manual setup. History import does not use app detection as a prerequisite. |
| Installer error | Read the steps printed by `bin/muninn-install`. It tries the checked repair path before it prepares the database. | Run the same command again. If it tells you to give a report, run its displayed health-report command. `recovery_required` keeps current data, runtime, and configuration, and keeps the service stopped. Keep all archives, the latest key, and intent. |
| Uninstall specified | Removal operation | `bin/muninn-uninstall --dry-run`, then `bin/muninn-uninstall`. The data moves to `~/.local/share/muninn-removed-<ts>` (`--purge-data` deletes it) |
| `bin/muninn-install` says half-installed after an uninstall | An uninstall stopped before it removed the data directory or release link. | Run `bin/muninn-uninstall` again. It removes the remaining parts. |


## Cursor history checks

Check `~/.cursor/hooks.json` against [Cursor setup](QUICKSTART.md#cursor-history).
Possible causes include a missing or disabled native hook and an unavailable database path.
They also include invalid `message_count`, missing or mismatched conversation identity, and invalid or ambiguous workspace roots.
An oversized conversation or refresh error can also prevent import.

Refresh without an error and a missing database each report `{}`.
An invalid payload identity, count, workspace, or import error reports `user_message`.
Check that Cursor supplies `conversation_id` and native absolute `workspace_roots`.
Muninn does not select a latest row as a fallback.

Use `muninn ingest --full` for the complete import.
`recall.off` controls subsequent prompt recall, not the refresh.
The hook's 90-second deadline does not guarantee that all hosts finish.

## Logs

- `poller.log`: one JSON line for each `event`.
  `start` records startup.
  `pass` records changed files or errors, and `idle` records hourly idle status.
  `error` records an `exception` class.
  `heartbeat_failed` records the error class when the alive timestamp could not be written.
  This happens one time for each pass and makes the poller `stale`.

  `crash` records the `exception` class when the poller ended between passes.
  The poller exits 1, and launchd starts it again.
  `stop` records a stop.
  `quiet_failed` records the error class if the poller could not redirect launchd output away from the log.
  At 1 MiB, the log moves to `poller.log.1`.

- `calls.jsonl`: allowlisted CLI and learning lifecycle IDs, finite codes, counts, timing and nullable `usage` only.
At the same limit, the log moves to its numbered file.

`Review` lines keep the byte bound.
Logging does not guarantee a record for each lifecycle change. It does not prove complete lifecycle coverage.
`stages` can include `miss_*` outcome flags (see `docs/REFERENCE.md`).
They are facts, not proof of a miss.
`MUNINN_NO_CALLLOG=1` stops the log.
- `status.json`: the last pass and heartbeat.

## Rebuild recovery

A rebuild refusal with `source_changed` means the source `original`, key, erasure journal, file identity or sidecar `state` changed or could not be checked.
A busy result can follow admission fencing. Examine `recovery_required` and the exit code.
`rebuild_ingest_incomplete` keeps the candidate and archive when one or more read `attempts` had an error or could not read data.
A correct SQLite `quick_check` alone does not prove a complete rebuild.

`publication_possible: true` means replacement was tried or an orphan leaves earlier exposure uncertain.
`replace_failed`, missing output or missing logs do not prove that the previous file stays live.
Keep the live file, candidate, all archives, `tombstone.key` and latest `tombstones.jsonl` as measured.
Do not put back an archive or promote an orphan automatically.

A new `process` refuses a current `muninn.sqlite.rebuild`.
It cannot infer a pre-crash admission value that was not recorded.
Resolve that recovery `state` before retrying or restarting consumers.

## Conversational memory choices

The agent handles checks and commands for this flow.
Ordinary accepted learning does not make approval necessary for each memory.
A related unresolved question or explicit `review` can start its own checked proposal.

| Result | Meaning and next step |
|---|---|
| `waiting_verification` with `waiting_index` | Muninn has not checked the `original` reply. Current background work can continue when indexing makes the record eligible. It has not recorded the memory. |
| `approved_pending` | Approval is checked. The memory has not committed. The agent can run the bounded targeted application. |
| `decision_display_limit` | The complete proposal and instructions exceed the recorded display limit. Muninn accepted no shortened display. The agent must prepare a complete proposal that fits. |
| `display_proof`, `owner_proof` | The complete unchanged `original` fields did not satisfy proof checks. The owner reply must come directly after the one display, with no message or tool call between. One reply backs one decision. A reply without the provider's owner label, a cropped field, or an agent claim cannot supply consent. |
| `owner_intent`, `decision_reference_required` | Older releases gave these for an owner reply that was not the fixed sentence. Current releases do not check the reply wording (ADR 0022). |
| `rejected_revision` | A target changed. Application refuses. The agent can prepare a new proposal when checked `evidence` permits it. |
| `rejected_evidence`, `rejected_erased` | Necessary `evidence` changed, is unavailable, or was erased. The agent must examine the specified finite refusal. It must not reconstruct erased content. |
| `already_saved` or `state` `saved` | The current receipt stays correct. A subsequent change needs its own checked retraction or replacement of recorded knowledge. |
| `rejected` | Checked refusal of the proposal recorded no knowledge through this decision. It does not put back withheld knowledge or automatically resolve a question. |
| `superseded` | The previous draft was replaced. The new draft needs a complete display and its own approval. |

An unverified negative intent does not cancel an accepted approval.
Status keeps `intent_state` different from the proposal's actual `state` and receipt.
A new draft prepared independently has its own identity and stays unapproved.
The agent reports the actual receipt or pending `state`.

A reply, queue entry, or launch is not recorded memory.

## A new project has no memory yet

A healthy new project can have no indexed project mapping.
The startup hook then reports usual empty project context.
It does not use global memory or a different project's knowledge as a replacement.
The indexer makes the mapping when it indexes a related `event`.

SessionEnd `requests` learning only from indexed eligible records.
It does not index, wait for indexing, or schedule a new startup `event`.
A subsequent startup, continue, clear, or compact can read the new mapping.
Prompt recall has its own conditions.

A correct prompt hook does not prove startup memory `delivery`.
Waiting 60 seconds alone does not refresh the current conversation's startup context.

Actual open, corruption, format, privacy, and nonregular-file refusals stay database errors.
`context_unavailable` means context generation had an error and no specified information was recorded.
The agent can examine the cause with the usual read-only checks.
It must not guess the cause of an older generic `error` record.
