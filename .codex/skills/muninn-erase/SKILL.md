---
name: muninn-erase
description: Forget stored content with `muninn erase`. Use only when the user explicitly asks to erase, forget or remove a session, event or text match from muninn memory (for example a leaked secret). Never run it on your own initiative.
allow_implicit_invocation: false
---

# muninn erase

    muninn erase --session S | --event REF | --match TEXT            # dry run (default)
    muninn erase --session S | --event REF | --match TEXT --yes      # actually erase

Erasure is permanent. It secure-deletes rows and full-text entries and writes tombstones (ids and hashes only) to prevent reimport, including from fork copies. `--match` searches event text and governed knowledge, reasons, expiry conditions, claims, evidence and unresolved content. Erasure closes over prior revisions and support/read associations, withdraws affected guides and fences review requests.

## Procedure

1. Run the dry run first and show the user the targets it lists. Do not add `--yes` yet.
2. Obtain explicit user approval for that exact target set. A broad `--match` may include unintended targets.
3. Explain that real erasure can interrupt the whole Muninn service, including unrelated ingestion, then re-run with `--yes`. It waits for selected-owner completion and contained process material before mutation; a replacement owner is refused rather than stopped too.
4. Report `not_covered` and `out_of_scope`: muninn does not erase provider transcript files, Time Machine backups or free disk blocks. Provide provider file paths if the user wants to remove those files. Do not delete provider transcripts yourself. If `aside_files` is not empty, those stores may still contain erased text. Show the user `aside_remove` (`rm --` on macOS/Linux, or PowerShell `Remove-Item -LiteralPath` with a literal path array on Windows) for manual execution; do not delete these stores yourself.
5. Report `erasure_status` and, when present, `vocab`, `residue` and `review.retained_archives`. If `stop_may_have_been_published` is true, status includes `restart="not_attempted"` and `next_action="check_and_restart_if_needed"`: have the owner check the service and arrange manual restart if needed, even after failure. Do not claim it is currently stopped or restored. Do not restart or reboot automatically. Run `muninn doctor` afterwards.

Real erasure owns the owner barrier before the writer barrier. Its unchanged Python
`wait_s=15.0` is one coordination-acquisition budget, not a total command, repair or
physical-I/O deadline. `wait_s=0` publishes no stop, records no repair baseline and
performs no sleeps; writer contention remains exit 3. There is no new CLI flag.
Dry runs use the old writer-only, no-stop path and may create an empty missing
store. Real missing-store erasure now refuses with exit 4 without creating it.
Neither path seeds containment evidence from absence or an empty database.

`erasure_status` is retained on handled success/failure. `repair_phase` is `none`,
`baseline_recorded`, `reconciling` or `complete`, independently of `erasure_phase`:
 `not_started`, `intent_may_be_durable`, `intent_durable`,
`committed`, then `verified` only after clean vocabulary/residue checks and zero
retained archives. Residue or any retained archive leaves `committed`; a completed
command with residue still exits 0. These bounded checks do not prove universal
forensic absence or erase provider/remote copies. Durable intent survives later
failure and is replayed on paused startup/erase entry. Pending intent fences new
foreground proposals/commits; existing receipts retain audit access until replay.
The guard reads the whole journal without a fixed memory/time bound. Preserve the
journal and `tombstone.key`.

Uncertain containment can require a bound repair baseline and a later supported
boot; the same boot refuses with `reboot_required`. Never delete witness/baseline
files to bypass it. Windows boot repair and retirement of an existing repair
baseline remain unsupported required-platform release blockers, not completed
recovery support. Native qualification remains separate.

Existing exits remain 3 for Busy; 4 for hot journal, tombstone-key, unavailable-store
or filesystem failures; 2 for value/lookup refusals. Original safe error details
and the latest status survive mapped failures. Arbitrary SQLite errors still
propagate; killed processes and failed output cannot guarantee a report.
