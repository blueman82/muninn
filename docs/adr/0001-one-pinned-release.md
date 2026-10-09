# 0001: Exactly one pinned release; upgrade in place

## Status

Accepted

Date: 2026-10-01

## Context

The launchd job and both hooks run `~/.local/lib/muninn/current/bin/muninn`. Superseded
release directories would otherwise accumulate.

## Decision

`~/.local/lib/muninn/` holds one release directory, the `current` and `python` links,
`install-record.json` and `install.log`. `install.installer --upgrade` selects a clean
commit, restarts the job with `launchctl kickstart -k`, verifies, and deletes every
other release as its last step.

## Consequences

After a successful upgrade the previous release is removed; no prior release is retained
for rollback. To return to an earlier release, select its commit with `--upgrade`,
subject to schema compatibility (ADR 0011). A failed upgrade rolls back before the
delete: it relinks the old release and restarts the job on it. Pruning runs last to
preserve failed-upgrade rollback: it only renames the old release aside (undoing the
renames if one fails), and the aside copies are deleted after the last point of
rollback, where a failed delete is only a warning. A failed upgrade also puts back the
pre-upgrade copy of the store when the new release had migrated its schema, since an
older release refuses a newer store.
