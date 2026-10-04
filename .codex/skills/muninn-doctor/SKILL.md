---
name: muninn-doctor
description: Run muninn health checks with `muninn doctor`. Use when muninn seems stale or broken, search returns nothing unexpectedly, hooks are not injecting, after an install or upgrade, or whenever the user asks if muninn is healthy.
---

# muninn doctor

    muninn doctor --pretty

Exit 0 and `ok: true` mean no error-level check failed; exit 1 means at least one did.

Each check has `ok` (true, false, or null = could not verify) and a `level`: `error` fails the run, `warn` is shown only, `info` is informational. Read `detail` for the cause.

Common fixes:
- `heartbeat` / `launchd_job` failing: the poller is not running; check the launchd job `com.muninn`.
- `file_modes` or `data_dir_mode`: data dir must be 0700 and files 0600 (a `recall.off` file must be 0600).
- `unexpected_files`: a stray file in the data dir; remove or move it.
- `unowned_journal`: a crashed writer's journal; the next writer rolls it back.
- `db_free_space` warn: run `muninn compact`.
- `tombstone_key` (error): `tombstone.key` is `missing` or `damaged` while erased content is tracked by it; ingest, rebuild and erase stop until it is restored from a backup. Do not recreate it by hand.
- `release_leftovers` (warn): a `.pruning-*` directory of an old release is still in `~/.local/lib/muninn`; the next `bin/muninn-install` removes it, or delete it by hand.
- `poller_error` (warn): the last pass stopped with an exception; `detail` is its class name. The poller's next good pass clears it (a manual `ingest` does not); if it stays, the poller keeps failing.
- `unreadable_files` (warn): the last pass could not open that many transcript files; check file permissions (macOS may need Full Disk Access for the poller).
- `reread` (info): `N of M` active sources are still to be re-read after an upgrade changed the classifier. It normally falls to `0 of M`; if it stays above 0, look at `failed_sources` and `stats` `last_pass.unreadable_files`. It never fails the run.
- `citations_resolve` warn: run `muninn know check`.

Report failing checks as found; do not mark them fixed until `doctor` is re-run and passes.
