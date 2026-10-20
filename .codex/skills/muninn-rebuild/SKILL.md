---
name: muninn-rebuild
description: Rebuild a verified current Muninn store from transcripts. Use only when the user explicitly asks to rebuild or re-derive the index, such as after a classifier change. Never run it on your own initiative.
disable-model-invocation: true
---

# muninn rebuild

    muninn rebuild

Replaces a verified marked current store after copying all governed history,
including normalized knowledge/revisions/support/receipts, scopes, tombstones,
unknown review charges and DB-only Cursor or missing-source events. It fully
re-ingests transcripts, replays latest erasure intent and validates the candidate.
Unreadable or historical unmarked stores refuse; rebuild does not migrate them.

Run only for an explicit rebuild request. Record `stats` and `doctor` baselines,
run rebuild, then compare counts and current knowledge with the baseline. Work
scales with transcript volume. Ingest counters include repeated committed work
and failed/open attempts across preparation passes, not a final-state census.

One owner spans sequential writer regions. Fixed nonmaterial Git preparation runs
outside the writer with SQLite handles closed. Changed source/key/intent/sidecar
state refuses as `source_changed`; failed or unreadable ingest refuses as
`rebuild_ingest_incomplete`. Exit 2 is refusal, 3 busy, and 4 unavailable or failed
publication. Check `recovery_required` on every failure, including busy.

`old_kept_as` names a retained private `muninn.sqlite.recovery-<id>` archive after
completed archive creation. Partial archives also remain as erasure residue.
`publication_possible` is true before replacement and stays true on success or
failure; an orphan candidate also preserves prior uncertainty. There is no
`old_restored` claim. Missing output/logs cannot establish nonpublication.

After failure preserve current live data, candidate, every archive, `tombstone.key`
and latest `tombstones.jsonl`. Never restore, promote an orphan, retry blindly or
infer a lost prior admission value. A fresh invocation refuses an existing
`muninn.sqlite.rebuild`. Archive deletion is a separate owner decision; archives
can retain erased content and prevent verified erasure. Native crash/Windows
qualification is separate from deterministic local tests.
