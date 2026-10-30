# Troubleshooting

Run `muninn --pretty doctor`. Each check has `ok`, a `level` (`error` makes the exit
code 1; `warn` and `info` do not) and a `detail`. When asking for help, share `doctor`,
`stats`, `install.log` and `install-record.json`. None of them holds transcript text.

| Symptom | Likely cause | What to do |
|---|---|---|
| `muninn: no Python 3.13+ found` | no interpreter on PATH or at the installer's link | set `MUNINN_PYTHON=/path/to/python3.13`, or re-run the installer under the right Python |
| `poller: stale` in every answer, `heartbeat` fails | the launchd job is down or stuck | `launchctl print gui/$(id -u)/com.muninn`; read `poller.log`; `launchctl kickstart -k gui/$(id -u)/com.muninn` |
| `poller: stale` while the poller runs | one source is taking longer than 3 intervals (the alive stamp is written between sources, not inside one), or the poller is on a release older than the alive stamp | wait for the pass to end (`muninn stats` shows `reread.pending` falling and `last_pass.alive_age_s` while it runs, and an `alive_age_s` that keeps growing means the poller died mid-pass and was not restarted; `poller.log` gets a line only when a pass finishes); a pass that keeps failing or finding the lock busy never stamps, so it reports `stale` |
| `launchd_job` fails | job not loaded | re-run `bin/muninn-install` (it upgrades an installed machine) |
| search finds little | few sources indexed | `muninn --pretty stats`: check `sources`, `events_by_provider` (a provider at 0 means its root was not found) and `last_pass.unreadable_files` (files the poller could not open) and `last_pass.skipped_files`; `muninn doctor` shows `roots_present` |
| `db_free_space` warns | many deletes left free pages | `muninn compact` (needs about one database's worth of free disk) |
| `aside_files` warns | historical unreadable files or retained rebuild archives, including partial copies, remain; erasure cannot scrub them | inspect the reported paths and preserve recovery evidence until its removal is explicitly authorized; `aside_remove` supplies literal removal commands, never automatic restoration |
| `upgrade_snapshot` warns | a governed pre-upgrade archive remains and can contain erased text | retain it while recovery is unresolved; only remove the named archive after an explicit owner decision, using the literal `aside_remove` command |
| exit 4, `error` `tombstone_key`; `doctor` `tombstone_key` fails (`missing` or `damaged`) | `tombstone.key` is missing or damaged while keyed tombstones exist; ingest, rebuild and erase refuse to run | restore `tombstone.key` from a backup. Do not recreate it: a new key stops the existing tombstones matching, so erased content could come back |
| `release_leftovers` warns | an upgrade's sweep could not delete an old `.pruning-<ts>-<sha>` release directory in `~/.local/lib/muninn` | the next `bin/muninn-install` retries the sweep; or delete the named directory by hand |
| `db_size` warns | database over 2 GiB | `muninn compact`; consider `muninn erase` of old sessions |
| exit 3, `busy` | another writer holds the lock (poller pass, compact, erase) | retry in a few seconds |
| `store_unavailable` / `cutover_required` (hooks say `memory unavailable`) | the current marker is absent, the schema is unsupported, or the store cannot be read | preserve the file; ordinary ingest and rebuild do not migrate historical schemas. `--upgrade` converts the supported schema v3 table contract; other legacy layouts need an explicitly supported recovery procedure |
| `restart_required` from legacy installer staging | durable managed launch exclusion is armed; no store has been copied or activated | preserve the record and exclusion; keep old unmanaged producers stopped, restart the OS, then repeat the same prepared checkout and full upgrade invocation |
| `legacy_cutover_pending` | later boot and unchanged exclusion were verified | the preparation-only verifier stops here; continue through the public installer with the same checkout, home and operation record |
| `legacy_recovery_required` / legacy `recovery_required` | exclusion, source, boot or durable operation evidence is incomplete or changed | retain source and private rollback directory; do not hand-restore the old runtime or reuse a previous boot baseline; inspect the exact failure before any recovery |
| `activation_pending` | no policy was selected, or the host does not meet the default runner requirements | Check for Apple silicon macOS, a signed `codex` CLI on `PATH`, and the canonical Codex sign-in directory. If one is missing, learning stays pending. Use a supported policy for another host. |
| `activation_incomplete` | a committed first policy is present but actual admission was fenced to zero | preserve control and boundaries; this modifier cannot reenable configured admission |
| `legacy_configuration_incomplete` | compatible selection or provider mutation may have occurred without a complete durable configuration record | keep the maintenance launcher and original snapshots; inspect/repair configuration before retrying; do not rerun plugin installation blindly |
| `review_policy_transition_required` | the selected policy cannot be installed or refreshed under the current rules | Keep the current policy and admission. Refresh a repository default only when admission is enabled. Confirm that no learning requests or knowledge entries exist. Use a later approval time. Change only fields allowed in [REFERENCE.md](REFERENCE.md#default-learning-policy). |
| `review_policy_snapshot_changed` | a retained policy snapshot or newly supplied input differs | preserve the original private record; resume without an external path only when its frozen snapshot is intact |

| exit 4, `hot_journal` / `store_unavailable` | a writer crashed mid-transaction, or a schema mismatch | run any writing command (`muninn ingest`) to roll the journal back; `doctor` shows `unowned_journal` |
| `unexpected_files` fails | a stray file in the data dir | move it out; `recall.off`, `recall.advisory` and the logs are expected |
| hooks print nothing | `MUNINN_HOOK_DISABLE=1`, `recall.off`, or an empty/slash prompt | unset the disable flag or select `recall.advisory` for model-led search instructions |
| Codex shows no memory | plugin hooks not trusted | start Codex, run `/hooks`, trust the two hooks |
| Cursor history is missing after compaction | optional native hook absent or disabled, database path unavailable, invalid `message_count`, missing/mismatched conversation identity, invalid/ambiguous workspace roots, oversized conversation, or refresh failure | check `~/.cursor/hooks.json` against [Cursor setup](QUICKSTART.md#cursor-history); outside macOS set `MUNINN_CURSOR_DB` in the Cursor environment. Successful refresh and a missing database return `{}`; invalid payload identity/count/workspace or import failure reports a `user_message`. Confirm Cursor supplies `conversation_id` and native absolute `workspace_roots`; no latest-row fallback is used. Use `muninn ingest --full` for the complete import; `recall.off` controls later prompt recall, not the refresh. The hook's 90-second deadline does not guarantee completion on every host |
| `app not installed: setup skipped`; no Cursor prompt | desktop app absent; leftover configs/transcripts insufficient | [Supported app detection](QUICKSTART.md#install-on-a-new-machine); installed Claude/Codex without config: manual setup; history import independent |
| installer failed | read the steps printed by `bin/muninn-install`. Before store preparation, it tries the qualified repair path. | If it requests a reboot, restart the computer and rerun the same command. If it requests a report, run the exact health-report command it prints. `recovery_required` keeps current data, runtime and configuration. It leaves the service stopped. Preserve all archives, the latest key and intent. |
| uninstall requested | removal operation | `bin/muninn-uninstall --dry-run`, then `bin/muninn-uninstall`; the data moves to `~/.local/share/muninn-removed-<ts>` (`--purge-data` deletes it) |
| `bin/muninn-install` says half-installed after an uninstall | an uninstall that was interrupted left the data dir or the release link | run `bin/muninn-uninstall` again; it removes whatever is left |


## Review health and recovery limits

`stats.review` reads durable request/call states, success receipts, indexed logical
record coverage and unresolved counts. Activation excludes the archive; explicit
historical replay preserves that baseline. Only success receipts with current indexed provenance count completed
work: the admitted endpoint must still match, and interior records need matching
live evidence linked to that same request. Endpoint equality alone proves no
interior content. Missing/changed/erased inputs remain uncertain gaps, reducing
metric availability while retaining immutable audit receipts. Health reads no
originals; it does not prove live physical content. Overlapping windows/multipart
records count once. Later explicit windows
can leave earlier gaps. Removed/erased index records are not lifetime coverage.
Unknown usage remains null, never a fabricated zero or reservation charge.
Unconcluded/unknown calls remain unknown even if they retain nonnull metadata.
Currency is reported only as a three-letter uppercase code; unsafe currency text
and its associated cost remain unknown.

`review_coverage_gaps` uses the same eligible set as stats, including explicitly
admitted historical replay, and warns when receipt/current provenance is missing.
Untouched activation archive stays excluded. `review_capture_gaps` warns about new
indexed records beyond admitted progress or uncertain prior admission provenance,
including refused reconciliation. These survive disabled or rotated logs. Exact
per-source refusal reasons do not: `observed_enqueue_busy` reports only retained
best-effort acknowledgments and `busy_count_complete` is always false. Later content
recovery cannot prove the lost event/cutoff. Cursor waits for its next native import
or explicit full ingest. Use `learn show R` for governed explanations; never add raw
exceptions/results, prompts, knowledge or credentials to health reports/logs.

`review_auth`/`review_config` count finite attempt-history failures. Check explicit
installed policy and admission before an operator-authorized retry.
`review_process_deadline` counts recorded deadline failures and overdue unfinished
attempts with unended calls;
it does not prove process liveness or completed termination. Commit and cancellation
can remain delayed after the process deadline (`review_commit_delay`,
`review_cancel_delay`). Original-source read budgets do not bound journal replay,
total I/O or command duration.

`review_pause` is always unverified. Witness presence, missing DB/PID or free
locks do not prove containment. A present repair-baseline path gives
`review_repair.detail: baseline_present_recovery_unverified`.
On macOS or Linux, `recovery_guidance.status: recovery_check_required` asks for
checked recovery. Doctor has not validated the record or its identity. A malformed
or unsafe record can still cause recovery to refuse.
Before an upgrade prepares the store, the installer tries the checked repair path on
macOS or Linux. If repair needs a reboot, restart the computer and rerun the
same install command. The installer then retries recovery.

When you run doctor yourself, run `muninn recover`.
Muninn waits for its managed writer and checks the saved boot, lock and witness
identities before repair. For a matching older macOS record without a disk ID,
the installer asks once before repair. The prompt says that Muninn cannot verify
the disk identity. Continue only if this is the Muninn data you intend to recover.
Muninn repeats all checks while it holds the maintenance lock.
If it returns `reboot_required`, restart the computer.
Run `muninn recover` again. After recovery, run `muninn doctor --pretty`.

Windows recovery is not qualified. `muninn recover` refuses before it changes
recovery files. Muninn cannot yet verify Windows boot identity or safe baseline
retirement. Keep the data folder and supervisor files.
Keep the repair baseline, `tombstones.jsonl`, tombstone keys and retained archives.
Save the full doctor report. Contact your Muninn maintainer.
On every platform, never remove or rename files to bypass
`pause_unverified` or `reboot_required`.

Three actions remain manual. Restart the computer if Muninn requests it.
Start the managed service if doctor reports it is stopped.
Contact the maintainer for Windows or identity-mismatch refusals.
Muninn does not reboot the computer because reboot interrupts other work.
`recover` does not revalidate the installed service definition before it
recommends a service start. A changed or unowned entry could run unexpected code.
Windows boot checks and baseline retirement have not passed native qualification.
If identity checks fail, Muninn preserves the files.
It cannot prove which process or store owns them.
Journal presence does not prove pending intent; consistency remains unverified.

On macOS, each new repair baseline records the data volume's persistent UUID.
After reboot, macOS may assign that volume a different device number.
Repair accepts this change only when the UUID, lock file and witness file match.
If macOS cannot provide a UUID, Muninn refuses to record a new baseline.
An older baseline has no UUID. The installer asks once when file numbers and
witness content still match. After approval, Muninn records the current volume
UUID and keeps the old boot ID. It then runs the existing reboot and repair checks.
Changed file numbers or witness content stop this recovery path.
The installer preserves all recovery files when a check fails.
Do not remove old files to force progress.

Real erase may interrupt the whole service. Preserve its original failure and
`erasure_status`: `restart: not_attempted` means check and manually restart if
needed. This field does not prove that the service is stopped or restored.
Retained archives keep erasure unverified, even if selected-needle scans find no residue. Windows boot
repair and durable baseline retirement remain unqualified. The doctor report
explains what to preserve and when to request help.

## Logs

- `poller.log`: one JSON line per event (`start`, `pass` when files changed or failed,
  hourly `idle`, `error` with an exception class, `heartbeat_failed` with the class of
  the error when the alive stamp could not be written (once per pass; the poller will
  then go `stale`), `crash` with the class of an exception that ended the poller outside
  a pass (it exits 1 and launchd restarts it), `stop`, and `quiet_failed` with the
  class of the error when the poller could not
  redirect launchd's output away from the log). Rotates at 1 MiB to `poller.log.1`.
- `calls.jsonl`: allowlisted CLI/review lifecycle IDs, finite codes, counts, timing and nullable usage only; rotates likewise. Review lines retain the byte bound. Logging is best effort, not complete lifecycle coverage.
  `stages` may carry `miss_*` outcome flags (see `docs/REFERENCE.md`); they are facts, not
  proof of a miss. `MUNINN_NO_CALLLOG=1` stops the log.
- `status.json`: the last pass and heartbeat.

## Rebuild recovery

A rebuild refusal with `source_changed` means the original source, key, erasure
journal, file identity or sidecar state changed or could not be verified. A busy
result can follow admission fencing: inspect `recovery_required` as well as the
exit code. `rebuild_ingest_incomplete` preserves the candidate and archive when
any attempt failed or was unreadable; a valid SQLite quick check alone is not
a complete rebuild.

`publication_possible: true` means replacement was attempted or an orphan leaves
earlier exposure uncertain. `replace_failed`, missing output or missing logs do
not prove that the old file remains live. Keep the live file, candidate, all
archives, `tombstone.key` and latest `tombstones.jsonl` as observed. Never restore
an archive or promote an orphan automatically. A fresh process refuses an existing
`muninn.sqlite.rebuild`; it cannot infer a lost pre-crash admission value.
Resolve that recovery state before retrying or restarting consumers.

## Conversational memory choices

The agent handles checks and commands for this flow.
Ordinary supported learning does not need approval for each memory.
A relevant unresolved question or explicit review can start a separate checked proposal.

| Result | Meaning and next action |
|---|---|
| `waiting_verification` with `waiting_index` | Muninn has not verified the original reply. Existing background work can continue when indexing makes the record eligible. It has not saved the memory. |
| `approved_pending` | Approval is verified. The memory has not committed. The agent can run the bounded targeted application. |
| `decision_display_limit` | The complete proposal and instructions exceed the saved display limit. Muninn accepted no shortened display. The agent must prepare a complete proposal that fits. |
| `display_proof`, `owner_proof`, `owner_intent` | The exact complete original fields did not pass proof checks. A wrapper, cropped field, or agent claim cannot supply consent. |
| `decision_reference_required` | A bare `approved` does not identify the complete proposal. Use the exact sentence from the complete display. |
| `rejected_revision` | A target changed. Application refuses. The agent can prepare a fresh proposal when checked evidence permits it. |
| `rejected_evidence`, `rejected_erased` | Required evidence changed, is unavailable, or was erased. The agent must investigate the specific finite refusal. It must not reconstruct erased content. |
| `learning_paused` | Views do not restart learning. Application keeps the pause in force. |
| `already_saved` or state `saved` | The existing receipt remains valid. A later change needs separate checked retraction or replacement of saved knowledge. |
| `rejected` | Verified rejection saved no knowledge through this decision. It does not restore withheld knowledge or automatically resolve a question. |
| `superseded` | The old draft was replaced. The new draft needs a complete display and its own approval. |

An unverified negative intent does not cancel an accepted approval.
Status keeps `intent_state` separate from the proposal's actual `state` and receipt.
An independent new draft remains separate and unapproved.
The agent reports the actual receipt or pending state. A reply, queue entry, or launch is not saved memory.

## Saved setup warnings

A setup warning can appear on startup, resume, clear, or compact.
The warning read does not check current host health or start a timer.
The background program can check again only while it runs and learning is enabled.
Its setup delay is at least 60 seconds, not an exact promised check time.
A failed check retains or replaces the warning. A successful relevant setup check or launch clears it.
Clearing a warning does not establish saved memory.

`codex_host_changed` means the saved computer ID does not match this computer.
Why that ID changed remains unknown.
The agent can inspect the saved approval. This does not renew it or change permissions.
`codex_data_home_changed`, `codex_platform_changed`, and `codex_project_required` identify different boundaries.
`codex_approval_required` and `codex_capability_status` identify missing approval or a changed runner status.
Older `codex_experimental_boundary` records did not retain the exact mismatch.
Muninn cannot infer it from that old code.
Human wording comes from safe current code, not a saved arbitrary message.

## A new project has no memory yet

A healthy new project can lack an indexed project mapping.
The startup hook then returns normal empty project context.
It does not use global memory or another project's knowledge instead.
The indexer creates the mapping when it indexes a relevant event.
SessionEnd requests learning only from already indexed eligible records.
It does not ingest, wait for indexing, or schedule a new startup event.
A later startup, resume, clear, or compact can read the new mapping.
Prompt recall remains separate and conditional.
A successful prompt hook does not prove startup memory delivery.
Waiting 60 seconds alone does not refresh the current conversation's startup context.

Real open, corruption, format, privacy, and nonregular-file refusals remain store failures.
`context_unavailable` instead means context generation failed and the exact detail was not recorded.
The agent can investigate with the normal read-only checks.
It must not guess the cause of an older generic `error` record.

### Changed Codex computer binding

`codex_host_changed` means the saved computer identity differs from the current
one. The cause of that change is unknown. The agent first checks format 2 and
existing recovery state, then prepares the explicit host-only candidate and
[non-writing preview](REFERENCE.md#renew-an-approved-codex-computer-binding).
Reading the notice or possessing the digest grants no permission and starts no
timer. Preserve an existing pause. A format-1 install must finish its ordinary
checked upgrade before renewal.

If the upgrade reports `host approval committed; upgrade incomplete`, the policy
grant remains committed. Resume only the same approved operation. Keep its
private receipt and archive. A missing, partial, oversized, changed or ambiguous
receipt refuses automatic continuation. Do not restore the archive over current
data, edit lock metadata or manufacture another approval. An unexplained lock
acquisition metadata gap requires operator or maintainer review; no automatic
continuation is supported for that gap.
