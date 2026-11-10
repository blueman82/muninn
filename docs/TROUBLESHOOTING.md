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
| `store_unavailable` / `cutover_required` (hooks say `memory unavailable`) | The database has no current marker, has a historical or unknown schema, or cannot be read. | Keep the file. Ordinary index and rebuild operations do not migrate historical schemas. `--upgrade` converts the accepted marked format 1 or the accepted schema v3 table contract. Other legacy layouts need a recovery procedure that explicitly accepts them. |
| `restart_required` from the legacy installer exclusion step | Durable managed launch exclusion is armed. No database has been copied or activated. | Keep the record and exclusion. Keep previous unmanaged producers stopped. Stop and start the OS. Then run the same prepared checkout and complete upgrade command again. |
| `legacy_cutover_pending` | Subsequent boot and unchanged exclusion were checked | The preparation-only verifier stops here. Continue through the public installer with the same checkout, home and operation record |
| `legacy_recovery_required` / legacy `recovery_required` | Exclusion, source, boot, or durable operation `evidence` changed or does not contain all necessary data. | Keep the source and private rollback directory. Do not put the previous runtime back manually or use a previous boot baseline again. Examine the reported error before recovery. |
| `activation_pending` | No policy was selected, or the host does not satisfy default runner requirements. | Check for Apple silicon macOS, a signed `codex` CLI on `PATH`, and the canonical Codex sign-in directory. If one is unavailable, learning stays pending. Use an accepted policy for a different host. |
| `activation_incomplete` | A committed first policy exists but actual admission was fenced to 0. | Keep control and boundaries. This modifier cannot reenable configured admission. |
| `legacy_configuration_incomplete` | Compatible selection or provider mutation could have occurred without a complete durable configuration record | Keep the maintenance launcher and `original` snapshots. Examine/repair configuration before retrying. Do not rerun plugin installation blindly |
| `review_policy_transition_required` | The selected policy cannot be installed or refreshed with the current rules. | Keep the current policy and admission. Refresh a repository default only with admission on. Check that no learning `requests` or knowledge entries exist. Use a subsequent approval time. Change only fields allowed in [Policy controls](REFERENCE.md#default-learning-policy). |
| `review_policy_snapshot_changed` | A kept policy snapshot or newly supplied input differs. | Keep the `original` private record. Continue without an external path only when the frozen snapshot is complete and unchanged. |

| Symptom | Likely cause | What to do |
|---|---|---|
| Exit 4, `hot_journal` / `store_unavailable` | A writer crashed during a transaction, or the schema differs. | Run a writing command, such as `muninn ingest`, to roll the journal back. `doctor` shows `unowned_journal`. |
| `unexpected_files` fails | A stray file exists in the data directory. | Move it out. `recall.off`, `recall.advisory`, and logs are permitted files. |
| Hooks print nothing | `MUNINN_HOOK_DISABLE=1`, `recall.off`, or an empty/slash prompt | Unset the disable flag or select `recall.advisory` for model-led search instructions |
| Codex shows no memory | Plugin hooks not trusted | Start Codex, run `/hooks`, trust the two hooks |
| Cursor history has no records after compaction | The optional native hook, database, payload, workspace, or refresh checks could not complete. | Use the [Cursor history checks](#cursor-history-checks) below. |
| `app not installed: setup skipped`. No Cursor prompt | No desktop app exists. Remaining configurations and transcripts do not prove installation. | See [App detection](QUICKSTART.md#install-on-a-new-machine). Installed Claude/Codex without configuration needs manual setup. History import does not use app detection as a prerequisite. |
| Installer error | Read the steps printed by `bin/muninn-install`. It tries the checked repair path before it prepares the database. | Stop and start the computer if the installer says a new boot is necessary. Run the same command again. If it tells you to give a report, run its displayed health-report command. `recovery_required` keeps current data, runtime, and configuration, and keeps the service stopped. Keep all archives, the latest key, and intent. |
| Uninstall specified | Removal operation | `bin/muninn-uninstall --dry-run`, then `bin/muninn-uninstall`. The data moves to `~/.local/share/muninn-removed-<ts>` (`--purge-data` deletes it) |
| `bin/muninn-install` says half-installed after an uninstall | An uninstall stopped before it removed the data directory or release link. | Run `bin/muninn-uninstall` again. It removes the remaining parts. |


## Cursor history checks

Check `~/.cursor/hooks.json` against [Cursor setup](QUICKSTART.md#cursor-history).
On platforms other than macOS, set `MUNINN_CURSOR_DB` in the Cursor environment.
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

## Review health and recovery limits

`stats.review` reads durable `request`/call `states`, success receipts, indexed logical record coverage and unresolved counts.
Activation excludes the archive.
Explicit historical replay keeps that baseline.
Only success receipts with current indexed provenance count completed work.
The admitted endpoint must be the same.
Records between the endpoints need matching live `evidence` linked to the same `request`.

Endpoint equality alone proves no content between the endpoints.
Missing/changed/erased inputs stay uncertain `gaps`, reducing metric availability while keeping immutable audit receipts.
Health reads no `originals`.
It does not prove live physical content.

Overlapping windows/multipart records count one time.
Subsequent explicit windows can keep earlier `gaps`.
Removed/erased index records are not lifetime coverage.
Unknown `usage` stays null, not a fabricated zero or reservation charge.

Unconcluded/unknown calls stay unknown if they keep nonnull metadata.
Currency is reported only as a three-letter uppercase code.
Invalid currency text and its related cost stay unknown.

`review_coverage_gaps` uses the same eligible set as stats, with explicitly admitted historical replay.
It reports a warning when there is no receipt or current provenance.
Untouched activation archive stays excluded.
`review_capture_gaps` warns about new indexed records after admitted `progress` or uncertain prior admission provenance, with refused reconciliation.
These survive disabled or rotated logs.

Per-source refusal causes do not survive: `observed_enqueue_busy` reports only kept acknowledgments and `busy_count_complete` is always `false`.
The acknowledgment record is not guaranteed.
Subsequent content recovery cannot prove an unrecorded `event` or cutoff.
Cursor waits for its next native import or explicit full index.
Use `learn show R` for governed explanations.

Do not add raw `exceptions`/results, prompts, knowledge or credentials to health reports/logs.

`review_auth`/`review_config` count finite attempt-history errors.
Check explicit installed policy and admission before an operator-authorized retry.
`review_process_deadline` counts recorded deadline errors and overdue unfinished `attempts` with unended calls.
It does not prove `process` liveness or completed termination.

Commit and cancellation can stay pending after the `process` deadline (`review_commit_delay`, `review_cancel_delay`).
Original-source read budgets do not bound journal replay, total I/O, or command time.

`review_pause` is always unverified.
A witness file, missing DB/PID, or free locks do not prove containment.
A repair-baseline path that exists gives `review_repair.detail: baseline_present_recovery_unverified`.
On macOS or Linux, `recovery_guidance.status: recovery_check_required` asks for checked recovery.

Doctor has not validated the record or its identity.
A malformed record or a record that fails identity or privacy checks can cause recovery to refuse.
Before an upgrade prepares the database, the installer tries the checked repair path on macOS or Linux.
If repair needs a reboot, restart the computer and rerun the same install command.

The installer then retries recovery.

Follow the recovery guidance from doctor before you run `muninn recover`.
Muninn waits for its managed writer and checks the recorded boot, lock and witness identities before repair.
For a matching older macOS record without a disk ID, the installer has one confirmation step before repair.
The prompt says that Muninn cannot check the disk identity.

Continue only if this is the Muninn data you intend to repair.
Muninn repeats all checks while it holds the maintenance lock.

A completed macOS owner with a matching volume UUID and lock inode needs no new boot after a device-number change.
For a legacy completed record without a UUID, `recover` can report `legacy_volume_confirmation_required`.
Check the intended Muninn data directory before using `--confirm-legacy-volume`.

The record cannot prove the initial disk identity.
Your confirmation supplies it.
For format 1, the installer checks completed erasure without writing that database before migration.
Pending historical erasure needs the checked format-1 recovery procedure first.
Recovery repeats its checks with the owner lock and writer lock held, applies recorded tombstones, and writes the current identity.
Active owners and changed records keep their refusal rules.

Before runtime selection, the installer keeps the legacy supervisor format.
The selected runtime adds the UUID at startup.
Rollback after selection keeps that runtime and leaves the service stopped.
A previous runtime can reject UUID fields.

Do not manually select a release that cannot read those fields.
Do not edit supervisor records.

If it reports `reboot_required`, restart the computer.
Run `muninn recover` again.
After recovery, run `muninn doctor --pretty`.

Windows recovery has no native qualification.
`muninn recover` refuses before it changes recovery files.
Muninn cannot check Windows boot identity or safe baseline retirement.
Keep the data folder and supervisor files.

Keep the repair baseline, `tombstones.jsonl`, tombstone keys and kept archives.
Keep the full doctor report.
Contact your Muninn maintainer.
On each platform, do not remove or rename files to bypass `pause_unverified` or `reboot_required`.

Three steps stay manual.
Stop and start the computer if Muninn says a new boot is necessary.
Start the managed service if doctor reports it is stopped.
Contact the maintainer for Windows or identity-mismatch refusals.

Muninn does not reboot the computer because reboot interrupts other work.
`recover` does not revalidate the installed service definition before it recommends a service start.
A changed or unowned entry could run unexpected code.
Windows boot checks and baseline retirement have no completed native qualification.

If identity checks fail, Muninn keeps the files.
It cannot prove which `process` or store owns them.
A journal file does not prove pending intent.
Consistency stays unverified.

On macOS, each new repair baseline records the data volume's permanent UUID.
After reboot, macOS can give that volume a different device number.
Repair accepts this change only when the UUID, lock file and witness file match.
If macOS cannot give a UUID, Muninn refuses to record a new baseline.

An older baseline has no UUID.
The installer asks one time when file numbers and witness content match.
After approval, Muninn records the current volume UUID and keeps the previous boot ID.
It then runs the current reboot and repair checks.

Changed file numbers or witness content stop this recovery path.
The installer keeps all recovery files when a check fails.
Do not remove previous files to bypass the refusal.

Live erase can stop the full service.
Keep its `original` error and `erasure_status`: `restart: not_attempted` means check and manually restart if needed.
This field does not prove that the service is stopped or put back.
Kept archives keep erasure unverified, when selected-needle scans find no `residue`.

Windows boot repair and durable baseline retirement stay unqualified.
The doctor report explains what to keep and when to `request` help.

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

- `calls.jsonl`: allowlisted CLI/`review` lifecycle IDs, finite codes, counts, timing and nullable `usage` only.
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
| `display_proof`, `owner_proof`, `owner_intent` | The complete unchanged `original` fields did not satisfy proof checks. A wrapper, cropped field, or agent claim cannot supply consent. |
| `decision_reference_required` | A bare `approved` does not identify the complete proposal. Use the unchanged sentence from the complete display. |
| `rejected_revision` | A target changed. Application refuses. The agent can prepare a new proposal when checked `evidence` permits it. |
| `rejected_evidence`, `rejected_erased` | Necessary `evidence` changed, is unavailable, or was erased. The agent must examine the specified finite refusal. It must not reconstruct erased content. |
| `learning_paused` | Views do not start learning again. Application keeps the pause in force. |
| `already_saved` or `state` `saved` | The current receipt stays correct. A subsequent change needs its own checked retraction or replacement of recorded knowledge. |
| `rejected` | Checked refusal of the proposal recorded no knowledge through this decision. It does not put back withheld knowledge or automatically resolve a question. |
| `superseded` | The previous draft was replaced. The new draft needs a complete display and its own approval. |

An unverified negative intent does not cancel an accepted approval.
Status keeps `intent_state` different from the proposal's actual `state` and receipt.
A new draft prepared independently has its own identity and stays unapproved.
The agent reports the actual receipt or pending `state`.

A reply, queue entry, or launch is not recorded memory.

## Saved setup warnings

A setup warning can show on startup, resume, clear, or compact.
The warning read does not check current host health or start a timer.
The background runner can check again only while it runs and learning is on.
Its setup wait is at minimum 60 seconds. No check time is guaranteed.

An unsuccessful check keeps or replaces the warning.
A correct related setup check or launch clears it.
Clearing a warning does not prove recorded memory.

`codex_host_changed` means the recorded computer ID differs from this computer.
Why that ID changed stays unknown.
The agent can examine the recorded approval.
This does not give a new grant or change permissions.

`codex_data_home_changed`, `codex_platform_changed`, and `codex_project_required` identify different boundaries.
`codex_approval_required` and `codex_capability_status` identify missing approval or a changed runner status.
Older `codex_experimental_boundary` records did not keep the specified mismatch.
Muninn cannot infer it from that previous code.

Human wording comes from safe current code, not a recorded arbitrary message.

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

### Changed Codex computer binding

`codex_host_changed` means the recorded computer identity differs from the current one.
The cause of that change is unknown.
The agent first checks format 2 and current recovery `state`, then prepares the explicit host-only candidate and [non-writing preview](REFERENCE.md#renew-an-approved-codex-computer-binding).
The notice and digest give no permission and start no timer.

Keep a current pause.
A format-1 install must finish its ordinary checked upgrade before renewal.

If the upgrade reports `host approval committed; upgrade incomplete`, the policy grant stays committed.
Continue only the same approved operation.
Keep its private receipt and archive.
A missing, unfinished, oversized, changed, or ambiguous receipt refuses automatic continuation.

Do not put back the archive above current data, edit lock metadata or manufacture a different approval.
An unexplained lock acquisition metadata `gap` needs operator or maintainer `review`.
No automatic continuation is accepted for that `gap`.

### Current Codex installation changed

A missing previous executable is not permission to broaden its recorded grant.
Prepare [the managed interface preview](REFERENCE.md#follow-the-current-trusted-codex-installation).
It checks the `original` approval and the current trusted manager.
The installer must get owner approval of the complete unchanged initial rule.

Compatible subsequent updates keep that grant without a different version question.

A missing selection, storage with access errors, an unaccepted interface, an unavailable isolated probe, and a structural limit have different safe codes.
A cache that fails access checks stays a refusal.
Safe invalid cache content needs all fixed isolated checks again.
`runtime_race_exhausted` stops that call after its one allowed pre-exec refresh.

It does not refund the reservation or add an automatic `attempt`.

If installation fails after the grant commits, keep its private receipt and the new grant paused.
Continue the same checked operation.
Do not put back an archive, edit the grant or restart older code against the managed JSON format.
A recovered installation can keep an admission pause.

Installation success does not prove that learning resumed or that a model used the expected billed account.
# Physical computer enrollment for managed Codex

If the previous Codex executable is gone and the previous computer digest differs, use the managed interface preview.
The previous digest includes the computer name.
Its change does not prove a change of physical computer.

```sh
bin/muninn-install --upgrade --codex-interface preview --check
```

The preview writes nothing.
It checks the previous approval files and shows a new physical target.
One new yes/y approval authorizes that target and the stable Codex runner rule.
Previous physical continuity stays unverified.

If Muninn reports `codex_host_identity_unavailable`, it cannot read a checked physical ID.
If it reports `codex_host_changed`, a checked physical ID differs.
Do not change the approval files or use a different identity source.
Keep the service stopped and give the finite code to the maintainer.

If Muninn reports `codex_interface_runtime_required`, use a release that can parse the new managed host terms.
The previous runner-selection marker cannot pass.
After a grant commits, run again the same checked operation with compatible code.
Do not start the service by hand or put back a previous grant.
