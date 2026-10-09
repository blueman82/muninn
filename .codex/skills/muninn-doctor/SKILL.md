---
name: muninn-doctor
description: Run muninn health checks with `muninn doctor`. Use when muninn seems stale or broken, search returns nothing unexpectedly, hooks are not injecting, after an install or upgrade, or whenever the user asks if muninn is healthy.
---

# muninn doctor

    muninn doctor --pretty

Exit 0 and `ok: true` mean no error-level check failed; exit 1 means at least one did.

Each check has `ok` (true, false, or null = could not verify) and a `level`: `error` fails the run, `warn` is shown only, `info` is informational. Read `detail` for the cause.

## Check responses

- `heartbeat` or `launchd_job` (macOS) / `managed_service` (Linux/Windows) failing: check the owned per-user service. A null service result means ownership or status could not be verified.
- `file_modes` or `data_dir_mode`: POSIX data directories require mode 0700 and files 0600; Windows requires private ACLs, which chmod does not establish.
- `unexpected_files`: remove or move unexpected files from the data directory.
- `unowned_journal`: a crashed writer's journal; the next writer rolls it back.
- `db_free_space` warn: run `muninn compact`.
- `tombstone_key` (error): `tombstone.key` is `missing` or `damaged` while erased content is tracked by it; ingest, rebuild and erase stop until it is restored from a backup. Do not recreate it by hand.
- `aside_files` (warn): `rebuild` set aside an unreadable store (`muninn.sqlite.unreadable-*`); `erase` cannot scrub it, so it may still hold erased text. Salvage required data, then delete the store manually.
- `upgrade_snapshot` (warn): a pre-upgrade copy of the store (`muninn.sqlite.pre-upgrade-*`) was left because the installer could not delete it; it holds transcript text and `erase` cannot scrub it. Delete it.
- `release_leftovers` (warn): an old release has a remaining `.pruning-*` directory in `~/.local/lib/muninn`; the next `bin/muninn-install` removes it, or remove it manually.
- `poller_error` (warn): the last pass stopped with an exception; `detail` is its class name. The next successful poller pass clears it; a manual `ingest` does not. A persistent warning indicates recurring poller failures.
- `unreadable_files` (warn): the last pass could not open that many transcript files; check file permissions (macOS may need Full Disk Access for the poller).
- `reread` (info): `N of M` active sources are still to be re-read after an upgrade changed the classifier. The count normally reaches `0 of M`. If it remains above 0, inspect `failed_sources` and `stats` `last_pass.unreadable_files`. It never fails the run.
- `citations_resolve` warn: run `muninn know check`.

Report failing checks. Claim a fix only after `doctor` is re-run and passes.
