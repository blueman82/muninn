---
name: muninn-doctor
description: Run Muninn health checks after installation, upgrade, or a reported failure.
---

# muninn doctor

```sh
muninn doctor --pretty
```

Exit 0 means no error-level check failed.
Exit 1 means an error-level check failed.
Warnings do not change the exit code.
Each check has `ok`, `level`, and `detail`.
A null `ok` means Muninn could not verify the condition.
Read `detail` before taking action.

## Service and storage checks

- `heartbeat`, `launchd_job`, or `managed_service`: check the owned per-user service.
  A null service result means ownership or status remains unverified.
- `file_modes` or `data_dir_mode`: POSIX directories require mode 0700 and files require mode 0600.
  Windows requires private ACLs.
  `chmod` does not establish those ACLs.
- `unexpected_files`: inspect the named files.
  Keep supervisor locks, witnesses, repair records, stop records, journals, keys, and retained archives.
  Never delete these files to bypass a refusal.
- `unowned_journal`: a crashed writer left a journal.
  The next writer rolls it back.
- `db_free_space`: run `muninn compact` when space recovery is needed.
- `tombstone_key`: a missing or damaged key blocks ingest, rebuild, and erase.
  Restore `tombstone.key` from a backup.
  Do not recreate the key by hand.
- `aside_files`: an unreadable retained store can still contain erased text.
  Salvage required data before deleting that retained store.
- `upgrade_snapshot`: the installer retains private pre-upgrade recovery archives.
  A successful upgrade does not remove them.
  They can contain transcript text, including the legacy knowledge ledger.
  `erase` cannot scrub this copy.
  Keep it until the owner reviews recovery and retention needs.
  Do not delete it just to clear this warning or restore it over newer data.
- `release_leftovers`: the next installer run removes leftover `.pruning-*` release directories.

## Ingest checks

- `poller_error`: the last pass raised an exception.
  `detail` gives its class name.
  A successful poller pass clears it.
  Manual ingest does not clear it.
- `failed_sources`: this counts failed read attempts during the last completed invocation.
  It can include sources that later recovered.
  Repeated attempts count separately.
  Preparation misses are excluded.
- `unreadable_files`: inspect permissions for the reported failed open attempts.
  The macOS poller can require Full Disk Access.
- `reread`: `N of M` counts active sources awaiting another read after a classifier change.
  If source reads succeed, the remaining count reaches zero.
  If the count remains positive, inspect `failed_sources` and `stats.last_pass.unreadable_files`.
  These fields count attempts, not distinct unresolved sources.

## Learning checks

- `review_backlog` reports queued requests.
- `review_policy` reports configured-policy presence and model validity.
  It does not establish that admission is open or the policy digest matches.
- `review_admission` reports stored admission prerequisites.
  `open` means the switch is on and the checked policy has a matching canonical digest.
  `closed` means the switch is off; this is informational, with `ok: false`.
  `closed_policy_absent`, `closed_policy_invalid`, or `closed_policy_digest_mismatch`
  also report policy integrity while admission is off.
  An open switch with `policy_absent`, `policy_invalid`, or `policy_digest_mismatch`
  is a warning.
  `control_absent` or `control_invalid` is a warning with `ok: null`.
  An open, valid control does not prove provider access, scanning, or learning success.
- `review_auth`, `review_config`, and `review_abandoned` report attempt history.
  Historical configuration errors do not establish a current admission failure.
  With no control row, `review_abandoned` is unknown with `generation_unavailable`;
  `review_capture_gaps` is unknown with `control_absent`.
  Independent history, backlog, coverage, and attention checks remain available.
- `review_attention` gives a fixed stage, cause, and action.
  The next Codex session also shows required action.
  These diagnostics contain no transcript text.
- `review_process_deadline` reports a recorded deadline failure or an overdue unfinished call.
  It does not prove a live process or a completed stop.
- `review_commit_delay` and `review_cancel_delay` report delayed finalisation.
- `review_coverage_gaps` and `review_capture_gaps` report eligible records without matching completion evidence.
  They include admitted historical replay and exclude untouched activation history.
  Endpoint equality cannot prove unchanged interior content.
  Missing, changed, or erased evidence remains a gap.
  Refusals and lost logs do not count as success.
  A capture gap does not identify its cause or prove a lost lifecycle event.
  New capture and catch-up log entries preserve `evidence_limit` for source-read refusals.
  Those limits include source traversal, not just selected text.
  A narrower window can still require reading earlier records.
  Do not raise limits or replay history merely to clear the count.
- `review_pause` reports witness presence.
  It does not verify process containment.
- `review_repair` reports a saved repair record.
- `review_erasure_intent` reports journal presence without proving consistency.
- `review_windows_repair` reports an unqualified Windows repair boundary.
  Keep all protocol evidence.

Doctor does not create, reset, or delete repair files.
An absent database, PID, or lock does not prove that related processes stopped.

## Recovery

If macOS or Linux reports `recovery_guidance`, run `muninn recover`.
If recovery returns `reboot_required`, restart the computer.
After restarting, run `muninn recover` again.
Run `muninn doctor --pretty` after recovery.
`recover` does not restart the service.
On Windows, keep all repair files.
Windows repair requires native qualification before release.

An erase operation can interrupt the service.
Read `erasure_status` before claiming that the service restarted.
If `restart` is `not_attempted`, check the service and restart it when required.
Retained archives prevent verified erasure.
Original-read limits do not limit journal work, total I/O, or command time.
Process deadlines do not limit database commit or cancellation delays.

Report failed checks.
Claim a repair only after a new doctor run passes.
