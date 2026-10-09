# 0011: Snapshot the store before a one-way schema upgrade

## Status

Accepted

Date: 2026-10-05

## Context

Schema v2 (typed ledger fields, tags, loop scope) migrates a v1 store in one transaction
the first time a v2 writer (the poller, `ingest` or `know add`) opens it; row ids and
the FTS index are kept. An older release refuses a v2 store (`StoreUnavailableError`,
"schema v2, need v1"), and the poller starts before `verify` passes. ADR 0001 requires
failed-upgrade rollback to the old release, which cannot open a store migrated to a
newer schema.

## Decision

The migration stays one-way. `--upgrade` preserves ADR 0001's failed-upgrade rollback by
copying the store first:

- Before the new release is pinned, `install/snapshot.py` reads the new release's
  `SCHEMA_VERSION` from git. If the store's `user_version` is lower, it copies the store
  with SQLite's backup API (the old poller may be writing) to
  `muninn.sqlite.pre-upgrade-<12-hex sha>`, mode 0600, and checks the copy. If the copy
  fails, the upgrade stops before anything changes.
- On a failed upgrade, rollback relinks the old release, then restores the copy only if
  the live store was actually migrated: it stops the job, removes a stale journal and
  renames the copy over the store. A store the new release never touched is kept, since
  it has newer ingests than the copy.
- A successful upgrade deletes the copy. `muninn doctor` warns while one is left
  (`upgrade_snapshot`), and `erase` names it in `aside_files` and `aside_remove`.

## Consequences

- The copy holds transcript text and the ledger until the upgrade ends. It is the reason
  for the 0600 mode, the delete and the `erase` report.
- A store that cannot be read or copied blocks an upgrade that would migrate it. Run
  `rebuild` first.
- Writes between the snapshot and rollback are lost on restore. Events can be re-derived
  from the transcripts; ledger entries added in that window do not.
- Returning to a pre-v2 release with `--upgrade` requires the snapshot because that
  release cannot read a v2 store. Any later schema bump is one-way in the same way.
- The rollback was tested against the installer fakes, not a live launchd run.
