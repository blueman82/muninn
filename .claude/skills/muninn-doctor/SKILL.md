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

- `heartbeat` or `launchd_job`: check the owned per-user service.
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
  An unavailable observation does not make a repair step necessary.

- `file_modes` or `data_dir_mode`: directories must have mode 0700 and files must have mode 0600.

- `unexpected_files`: examine the named files.
  Keep journals, keys, and archives.
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

These checks do not prove provider access, scanning, or learning success.
They read no transcript text.

- `review_backlog` reports queued learning work.

- `review_admission` reports `open` when learning is on and `closed` when it is off.
  This is informational.

- `review_auth` and `review_config` report attempt-history counts with those finite error codes.

- `review_process_deadline` reports a recorded deadline error or an unfinished attempt past the deadline.
  It does not prove that a related OS task is active or that it stopped.

- `review_commit_delay` and `review_cancel_delay` report finalisation that did not finish on time.

- `review_abandoned` reports abandoned attempts, or unfinished attempts from an older generation.

- `review_coverage_gaps` and `review_capture_gaps` report eligible records without corresponding proof that the work completed.
  Equal endpoints cannot prove that content between them did not change.
  Missing, changed, or erased source records leave coverage unproved.
  Capture-gap counts do not identify refusal causes.
  New capture and catch-up log entries keep `evidence_limit` for source-read refusals.
  Those limits include traversal of the counted records and selected text.
  For JSONL sources, records before the window start do not count.
  Cursor sources count all records from the first record.
  Do not increase limits only to clear the count.

- `review_erasure_intent` reports if a `tombstones.jsonl` journal exists without proving consistency.

Doctor does not make, reset, or remove files.
A missing database, PID, or lock does not prove that related OS tasks stopped.
Report checks that did not succeed.
Claim a repair only after a new doctor run passes.
