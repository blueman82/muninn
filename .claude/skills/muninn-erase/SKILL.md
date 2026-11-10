---
name: muninn-erase
description: Forget stored content with `muninn erase`. Use only when the user explicitly asks to erase, forget or remove a session, event or text match from muninn memory (for example a leaked secret). Never run it on your own initiative.
disable-model-invocation: true
---

# muninn erase

Preview is the default:

```sh
muninn erase --session S | --event REF | --match TEXT
```

Apply the specified erasure:

```sh
muninn erase --session S | --event REF | --match TEXT --yes
```

Erasure is permanent.
It secure-deletes rows and full-text entries.
It writes tombstones with IDs and hashes only to prevent reimport from sources and fork copies.
`--match` searches `event` text, governed knowledge, `reason` values, expiry conditions, claims, `evidence`, and unresolved content.

Erasure follows prior revisions and `support`/read links.
It removes affected `guides` from current use and fences `review` `requests`.
It clears candidate payloads and each affected decision locator, with pending intents before `evidence` admission.
Accepted revision metadata and historical receipts stay, but cannot put an erased body back.

Current `evidence` keeps checked source-path and selector metadata.
Do not claim that all possible provenance strings are content-free.

## Procedure

1. Run the dry run and show the listed targets.

2. Get the user's approval for that unchanged target set.

3. If `--match` is broad, warn that it can select unintended targets.

4. Tell the user that live erasure can stop all Muninn work, with ingestion.

5. After approval, run the same command with `--yes`.

6. Report `not_covered`, `out_of_scope`, and `erasure_status`.

7. Also report `vocab`, `residue`, and `review.retained_archives` if the result has them.

8. Report that Muninn does not erase provider transcripts, Time Machine recovery copies, or free disk blocks.

9. If the user wants provider transcript files removed, give their paths.

10. Do not remove provider transcripts.

11. If `aside_files` is not empty, warn that those databases can contain erased text.

12. Show the `aside_remove` command for manual use.

13. On macOS or Linux, use `rm --` with the listed paths.

14. On Windows, use PowerShell `Remove-Item -LiteralPath` with a literal path array.

15. Tell the user to remove aside databases.

16. Do not remove aside databases.

17. If `stop_may_have_been_published` is `true`, report `restart="not_attempted"` and `next_action="check_and_restart_if_needed"`.

18. The system service manager can start Muninn again.

19. Muninn does not start the service again itself.

20. Do not claim that the service is stopped or active again.

21. Run `muninn doctor --pretty` after erasure.

22. On macOS or Linux, run `muninn recover` if doctor reports `recovery_guidance`.

23. On Windows, keep repair files.

24. Keep the report.

25. Contact the maintainer.

26. Do not reboot the computer automatically.

Live erasure gets the owner barrier before the writer barrier.
Its unchanged Python `wait_s=15.0` is one coordination-acquisition budget.
It does not limit total command time, repair time, or physical I/O time.
`wait_s=0` publishes no stop, records no repair baseline, and does not sleep.

Writer contention stays exit 3.
There is no new CLI flag.
Dry runs use the previous writer-only path with no stop and can make an empty database when no database is on disk.
Live erasure of a missing database refuses with exit 4 without making one.

A missing database gives no containment proof.
An empty database gives no containment proof.

Success and error results that Muninn handles keep `erasure_status`.
`repair_phase` is `none`, `baseline_recorded`, `reconciling`, or `complete`.
It has a value independently of `erasure_phase`.

`erasure_phase` starts at `not_started`.
It goes through `intent_may_be_durable`, `intent_durable`, and `committed`.
Only clean vocabulary/`residue` checks and zero kept archives permit `verified`.
`Residue` or a kept archive leaves the phase `committed`.

A completed command with `residue` exits 0.
These bounded checks do not prove that all forensic signs are gone.
They do not erase provider or remote copies.
Durable intent survives a subsequent error and replays on paused startup or erase entry.

Pending intent fences new foreground proposals and commits.
Current receipts keep audit access until replay.
The guard reads the full journal without a constant memory or time bound.
Keep the journal and `tombstone.key`.

Uncertain containment can make a bound repair baseline and a subsequent accepted boot necessary.
The same boot refuses with `reboot_required`.
Do not remove witness or baseline files to bypass that refusal.
Windows boot repair and retirement of a current repair baseline stay unaccepted required-platform release blockers.

They are not completed recovery `support`.
Native `qualification` has its own checks.

Current mapped exits stay 3 for Busy.
They stay 4 for hot journals, tombstone keys, unavailable databases, and filesystem errors.
They stay 2 for value or lookup refusals.
Mapped errors keep the initial safe error information and latest status.

Arbitrary SQLite errors propagate.
A killed `process` or an output error can prevent a report.
