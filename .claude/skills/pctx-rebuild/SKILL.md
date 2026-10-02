---
name: pctx-rebuild
description: Rebuild the pctx store from transcripts with `pctx rebuild`. Use only when the user explicitly asks to rebuild or re-derive the index (for example after a classifier change or a corrupt store). Never run it on your own initiative.
disable-model-invocation: true
---

# pctx rebuild

    pctx rebuild

Builds a new store from the provider transcripts, then copies over what cannot be re-derived: scopes, tombstones, the knowledge ledger (entries, citations, log) and the events of sources whose files have gone missing.

Why the care: it replaces the live database and takes time proportional to all transcripts.

## Procedure
1. Run `pctx stats` and `pctx doctor` first so there is a before picture.
2. Tell the user it replaces the index and get an explicit yes.
3. Run it. Exit code 3 means busy (retry); 4 means the store is unavailable.
4. Run `pctx doctor`, then compare `pctx stats` counts and `pctx know check` with the before picture. Report any differences instead of assuming parity.

Erased content stays erased because tombstones are carried over and checked before any line is read.
