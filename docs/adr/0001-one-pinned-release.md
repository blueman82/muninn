# 0001: Exactly one pinned release; upgrade in place

Status: Accepted, 2026-10-01. Decided by: owner ("only have one pinned release, nothing else").

**Context.** The launchd job and both hooks run `~/.local/lib/muninn/current/bin/muninn`. Superseded
release directories would otherwise accumulate.

**Decision.** `~/.local/lib/muninn/` holds one release directory, the `current` and `python` links,
`install-record.json` and `install.log`. `install.installer --upgrade` re-pins a clean commit, restarts the job with
`launchctl kickstart -k`, verifies, and deletes every other release as its last step.

**Consequences.** After a successful upgrade the previous release is gone, so there is no rollback; re-pin an earlier
commit with `--upgrade` to go back. A failed upgrade rolls back before the delete: it relinks the old release and restarts
the job on it. Pruning runs last so a failure can always return: it only renames the old release aside (undoing the renames if one
fails), and the aside copies are deleted after the last point of rollback, where a failed delete is only a warning. A failed upgrade also puts back the pre-upgrade copy of the store when the new release had migrated its schema, since an older release refuses a newer store.
