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
- `reread` (info): `N of M` sources are still being re-read after an upgrade changed the classifier; it falls to `0 of M` on its own and never fails the run.
- `citations_resolve` warn: run `muninn know check`.

Report failing checks as found; do not mark them fixed until `doctor` is re-run and passes.
