---
name: muninn-doctor
description: Run Muninn health checks after installation, upgrade, or a reported error.
---

# muninn doctor

```sh
muninn doctor --pretty
```

Exit 0 means no check reported an error-level problem.
Exit 1 means a check reported an error-level problem.
Warnings do not change the exit code.
Each check has `ok`, `level`, and `detail`.
A null `ok` means Muninn could not check the condition.
Read `detail` before taking a step.

## Service and storage checks

- `heartbeat`, `launchd_job`, or `managed_service`: check the owned per-user service.
  A null service result means ownership or status remains unverified.
  This result does not prove that the service is active.
  Exit 0 does not change this limit.

  On macOS, `launchd_job` passes only when launchctl reports one positive PID
  and a PID-only system query completes without error and gives the same PID.

  Unverified observations give null with `launchctl_unavailable`,
  `launchctl_pid_unavailable`, `process_inspection_unavailable`,
  `process_inspection_timeout`, `process_inspection_failed`, or
  `process_pid_unverified`.
  `launchctl_timeout` and `launchctl_failed` give `false`.

  An error from a launchctl query alone does not prove that the service stopped.
  Each native command has a 60-second limit.
  The report contains no native messages or OS task command lines.
  The other checks remain in the report.
  An unavailable observation does not make a recovery step necessary.

- `file_modes` or `data_dir_mode`: POSIX directories must have mode 0700 and files must have mode 0600.
  Windows must have private ACLs.
  `chmod` does not prove those ACLs.

- `unexpected_files`: examine the named files.
  Keep supervisor locks, witnesses, repair records, stop records, journals, keys, and archives.
  Do not remove these files to bypass a refusal.

- `unowned_journal`: a writer crashed and left a journal.
  The next writer rolls it back.

- `db_free_space`: run `muninn compact` when space recovery is necessary.

- `tombstone_key`: a missing or damaged key blocks ingest, rebuild, and erase.
  Replace `tombstone.key` with the key from a recovery copy.
  Do not recreate the key by hand.

- `aside_files`: a store that Muninn kept but cannot read can contain erased text.
  Collect necessary data before removing that store.

- `upgrade_snapshot`: the installer keeps private archives from before the upgrade for recovery.
  An upgrade that ends without error does not remove them.
  They can contain transcript text and the legacy knowledge ledger.
  `erase` cannot scrub this copy.
  Keep it until the owner examines recovery and retention needs.
  Do not remove it only to clear this warning.
  Do not replace newer data with this copy.

- `release_leftovers`: the next installer run removes leftover `.pruning-*` release directories.

## Ingest checks

- `poller_error`: the last pass raised an error.
  `detail` gives its class name.
  A poller pass that ends without error clears it.
  Manual ingest does not clear it.

- `failed_sources`: this counts read tries with errors during the last completed invocation.
  It can include sources that subsequently recovered.
  Each try counts independently.
  It does not count misses during preparatory work.

- `unreadable_files`: examine permissions for the reported open tries with errors.
  The macOS poller can make Full Disk Access necessary.

- `reread`: `N of M` counts active sources that await one more read after a classifier change.
  If source reads succeed, the remaining count reaches 0.
  If the count remains positive, examine `failed_sources` and `stats.last_pass.unreadable_files`.
  These fields count tries, not unique unresolved sources.

## Learning checks

- `review_backlog` reports queued learning work.

- `review_policy` reports if a configured policy exists and passes model validation.
  It does not prove that admission is open or that the policy digest agrees.

- `review_admission` reports stored admission prerequisites.
  `open` means the switch is on and the checked policy has the same canonical digest.
  `closed` means the switch is off.
  This is informational, with `ok: false`.
  `closed_policy_absent`, `closed_policy_invalid`, or `closed_policy_digest_mismatch`
  also report policy integrity while admission is off.

  An open switch with `policy_absent`, `policy_invalid`, or `policy_digest_mismatch`
  is a warning.
  `control_absent` or `control_invalid` is a warning with `ok: null`.
  An open control that passes validation does not prove provider access, scanning, or learning success.

- `review_auth`, `review_config`, and `review_abandoned` report try history.
  Historical configuration errors do not prove a current admission error.
  With no control row, `review_abandoned` is unknown with `generation_unavailable`.
  `review_capture_gaps` is unknown with `control_absent`.
  History, backlog, coverage, and owner-step checks remain available independently.

- `review_attention` gives a fixed phase, cause, and necessary step.
  The next Codex session also shows the necessary step.
  These diagnostics contain no transcript text.

- `review_process_deadline` reports a recorded deadline error or an overdue unfinished call.
  It does not prove that a related OS task is active or that it stopped.

- `review_commit_delay` and `review_cancel_delay` report finalisation that did not finish on time.

- `review_coverage_gaps` and `review_capture_gaps` report eligible records without corresponding proof that the work completed.
  They include admitted historical replay and exclude untouched activation history.
  Equal endpoints cannot prove that content between them did not change.
  Missing, changed, or erased source records leave coverage unproved.
  Refusals and missing logs do not count as success.

  A capture omission does not identify its cause or prove a missing lifecycle notification.
  New capture and catch-up log entries keep `evidence_limit` for source-read refusals.
  Those limits include source traversal and selected text.
  A narrower window can make reads of earlier records necessary.
  Do not increase limits or replay history merely to clear the count.

- `review_pause` reports if a witness exists.
  It does not prove OS task containment.

- `review_repair` reports a stored repair record.

- `review_erasure_intent` reports if a journal exists without proving consistency.

- `review_windows_repair` reports an unqualified Windows repair boundary.
  Keep all protocol proof records.

Doctor does not make, reset, or remove repair files.
A missing database, PID, or lock does not prove that related OS tasks stopped.

## Recovery

If macOS or Linux reports `recovery_guidance`, run `muninn recover`.
If recovery reports `reboot_required`, start the computer again.
After the computer starts, run `muninn recover` again.
Run `muninn doctor --pretty` after recovery.
`recover` does not start the service again.
On Windows, keep all repair files.

Windows repair must have native qualification before release.
An erase operation can stop the service.
Read `erasure_status` before claiming that the service started again.
If `restart` is `not_attempted`, check the service and start it again when necessary.
Archives that remain prevent proved erasure.
Limits on source reads do not limit journal work, total I/O, or command time.

OS task deadlines do not limit database commit or cancellation wait times.
Report checks that did not succeed.
Claim a repair only after a new doctor run passes.

For `codex_host_changed`, check current-format 2 and outstanding recovery first.
An installed format-1 store must have the ordinary checked upgrade before renewal.
That prerequisite grants no host permission.
Prepare a candidate that changes only its stored host field.
Then use `bin/muninn-install --upgrade --review-policy PATH
--renew-codex-host preview --check`.

The preview writes nothing in all flag positions.

Get the owner's new affirmative installer answer for that same plan before applying its digest.
Disclose the full upgrade, Codex hook checks, and possible background learning before the command ends.
Also disclose ongoing permission without an aggregate call or spending cap, and unverified provider billing and runner/host separation.
Keep pauses and previous work.

A notice grants no permission and starts no timer.
A committed grant remains in force if the upgrade subsequently ends with an error.
Keep its receipt and archive.
Continue only that same operation.
Missing proof or an unexplained break in lock continuity makes maintainer inspection necessary.
Do not edit receipts, replace current data with an archive, or make a new grant to bypass that inspection.
