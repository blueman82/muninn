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
| `review_policy_required` (SessionStart hook) | no valid active review policy is installed | install only an explicitly approved policy; Muninn has no default policy |
| `restart_required` from legacy installer staging | durable managed launch exclusion is armed; no store has been copied or activated | preserve the record and exclusion; keep old unmanaged producers stopped, restart the OS, then repeat the same prepared checkout and full upgrade invocation |
| `legacy_cutover_pending` | later boot and unchanged exclusion were verified | the preparation-only verifier stops here; continue through the public installer with the same checkout, home and operation record |
| `legacy_recovery_required` / legacy `recovery_required` | exclusion, source, boot or durable operation evidence is incomplete or changed | retain source and private rollback directory; do not hand-restore the old runtime or reuse a previous boot baseline; inspect the exact failure before any recovery |
| `activation_pending` | ordinary setup completed without a first review policy | provide an explicitly approved policy through the full `--upgrade --review-policy PATH` invocation when activation is authorized |
| `activation_incomplete` | a committed first policy is present but actual admission was fenced to zero | preserve control and boundaries; this modifier cannot reenable configured admission |
| `legacy_configuration_incomplete` | compatible selection or provider mutation may have occurred without a complete durable configuration record | keep the maintenance launcher and original snapshots; inspect/repair configuration before retrying; do not rerun plugin installation blindly |
| `review_policy_transition_required` | explicit input differs from the installed immutable policy | preserve the existing policy; replacement needs a separately authorized fenced transition |
| `review_policy_snapshot_changed` | a retained policy snapshot or newly supplied input differs | preserve the original private record; resume without an external path only when its frozen snapshot is intact |

| exit 4, `hot_journal` / `store_unavailable` | a writer crashed mid-transaction, or a schema mismatch | run any writing command (`muninn ingest`) to roll the journal back; `doctor` shows `unowned_journal` |
| `unexpected_files` fails | a stray file in the data dir | move it out; `recall.off`, `recall.advisory` and the logs are expected |
| hooks print nothing | `MUNINN_HOOK_DISABLE=1`, `recall.off`, or an empty/slash prompt | unset the disable flag or select `recall.advisory` for model-led search instructions |
| Codex shows no memory | plugin hooks not trusted | start Codex, run `/hooks`, trust the two hooks |
| Cursor history is missing after compaction | optional native hook absent or disabled, database path unavailable, invalid `message_count`, missing/mismatched conversation identity, invalid/ambiguous workspace roots, oversized conversation, or refresh failure | check `~/.cursor/hooks.json` against [Cursor setup](QUICKSTART.md#cursor-history); outside macOS set `MUNINN_CURSOR_DB` in the Cursor environment. Successful refresh and a missing database return `{}`; invalid payload identity/count/workspace or import failure reports a `user_message`. Confirm Cursor supplies `conversation_id` and native absolute `workspace_roots`; no latest-row fallback is used. Use `muninn ingest --full` for the complete import; `recall.off` controls later prompt recall, not the refresh. The hook's 90-second deadline does not guarantee completion on every host |
| `app not installed: setup skipped`; no Cursor prompt | desktop app absent; leftover configs/transcripts insufficient | [Supported app detection](QUICKSTART.md#install-on-a-new-machine); installed Claude/Codex without config: manual setup; history import independent |
| installer failed | inspect `install.log` and `install-record.json` (`outcome`, `failed`, `store.phase`) | `recovery_required` after possible current mutation keeps current data and runtime/configuration and leaves the service stopped. Preserve all archives and latest key/intent; do not assume rollback or restart succeeded |
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

`review_quiescence` is always unverified: witness presence, missing DB/PID and free
locks do not prove containment. Doctor never creates, resets or deletes
`review.supervisor.lock`, `review.supervisor.json` or
`review.supervisor.repair.json`. Preserve these, `tombstones.jsonl`, tombstone keys
and retained archives. A repair baseline needs manual recovery assessment; never
remove it to bypass `quiescence_unverified`/`reboot_required`. Journal presence is
reported with consistency unverified, not as proof of a pending intent.

Real erase may interrupt the whole service. Preserve its original failure and
`erasure_status`: `restart: not_attempted` means check and manually restart if
needed, with no claim that it is currently stopped/restored. Retained archives keep
erasure unverified even if selected-needle scans find no residue. Windows boot repair
and durable baseline retirement remain required-platform release blockers; ordinary
absent-baseline behavior and fake tests are not native qualification.

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
