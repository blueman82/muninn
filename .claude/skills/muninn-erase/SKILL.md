---
name: muninn-erase
description: Forget stored content with `muninn erase`. Use only when the user explicitly asks to erase, forget or remove a session, event or text match from muninn memory (for example a leaked secret). Never run it on your own initiative.
disable-model-invocation: true
---

# muninn erase

    muninn erase --session S | --event REF | --match TEXT            # dry run (default)
    muninn erase --session S | --event REF | --match TEXT --yes      # actually erase

Erasure is permanent. It secure-deletes rows and full-text entries and writes tombstones (ids and hashes only) to prevent reimport, including from fork copies. `--match` searches event text and governed knowledge, reasons, expiry conditions, claims, evidence and unresolved content. Erasure closes over prior revisions and support/read associations, withdraws affected guides and fences review requests.

## Procedure

1. Run the dry run and show the listed targets.
2. Ask the user to approve that exact target set.
3. If `--match` is broad, warn that it may select unintended targets.
4. Explain that real erasure can interrupt all Muninn work, including ingestion.
5. After approval, rerun the exact command with `--yes`.
6. Report `not_covered`, `out_of_scope`, and `erasure_status`.
7. Also report `vocab`, `residue` and `review.retained_archives` when present.
8. State that Muninn does not erase provider transcripts, Time Machine backups or free disk blocks.
9. If the user wants provider transcript files removed, provide their paths.
10. Do not delete provider transcripts yourself.
11. If `aside_files` is not empty, warn that those stores may contain erased text.
12. Show the `aside_remove` command for manual use.
13. On macOS or Linux, use `rm --` with the listed paths.
14. On Windows, use PowerShell `Remove-Item -LiteralPath` with a literal path array.
15. Ask the user to remove aside stores.
16. Do not remove aside stores yourself.
17. If `stop_may_have_been_published` is true, report both `restart="not_attempted"` and `next_action="check_and_restart_if_needed"`.
18. The system service manager may restart Muninn.
19. Muninn does not restart the service itself.
20. Do not claim that the service is stopped or restored.
21. Run `muninn doctor --pretty` after erasure.
22. On macOS or Linux, run `muninn recover` if doctor returns `recovery_guidance`.
23. On Windows, preserve repair files.
24. Save the report.
25. Contact the maintainer.
26. Never reboot the computer automatically.

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
