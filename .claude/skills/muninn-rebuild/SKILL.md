---
name: muninn-rebuild
description: Rebuild the muninn store from transcripts with `muninn rebuild`. Use only when the user explicitly asks to rebuild or re-derive the index (for example after a classifier change or a corrupt store). Never run it on your own initiative.
disable-model-invocation: true
---

# muninn rebuild

    muninn rebuild

Builds a new store from provider transcripts and preserves data that cannot be re-derived: scopes, tombstones, the knowledge ledger (entries, citations, log) and the events of sources whose files have gone missing.

Replaces the live database. Runtime scales with the total transcript volume.

## Procedure

1. Record baseline results from `muninn stats` and `muninn doctor`.
2. Explain that rebuilding replaces the index and obtain explicit user approval.
3. Run it. Exit code 3 means busy (retry); 4 means the store is unavailable.
4. Run `muninn doctor`, then compare `muninn stats` counts and `muninn know check` with the baseline. Report any differences.

An unreadable old store is preserved as `muninn.sqlite.unreadable-<UTC timestamp>` and reported in `old_kept_as`. Report the path; deletion is manual.

Erased content stays erased because tombstones are carried over and checked before any line is read.
