# 0002: The installer has only `--fresh` and `--upgrade`

Status: Accepted, 2026-10-01. Decided by: owner ("we're using this new only and never going back at all. Remove").

**Context.** `install/cutover.py` migrated this machine from the old daemon: it built a second data directory, swapped
the launchd job, moved the old data aside and could restore the old daemon. That migration is finished, the old tree is
deleted, and a colleague's machine has none of it.

**Decision.** Delete the legacy cutover and its rollback path, `doctor --cutover`, `PCTX_OLD_TREE` and the old-tree
checks. `install/installer.py` runs `--fresh` (new machine) or `--upgrade` (re-pin). One of the two is required.
`install/rollback.py` only undoes an interrupted run from its `rollback-record.json`.

**Consequences.** The old daemon cannot be restored by this code (git history keeps it). `classify.LEGACY_MARKERS`
stays: it flags the old daemon's injected blocks when they appear inside past transcripts.
