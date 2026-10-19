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
- `unexpected_files`: inspect unexpected files. The supervisor lock/witness/repair baseline, stop record, erasure journal, keys and retained archives are governed state; never delete them to bypass a refusal.
- `unowned_journal`: a crashed writer's journal; the next writer rolls it back.
- `db_free_space` warn: run `muninn compact`.
- `tombstone_key` (error): `tombstone.key` is `missing` or `damaged` while erased content is tracked by it; ingest, rebuild and erase stop until it is restored from a backup. Do not recreate it by hand.
- `aside_files` (warn): `rebuild` set aside an unreadable store (`muninn.sqlite.unreadable-*`); `erase` cannot scrub it, so it may still hold erased text. Salvage required data, then delete the store manually.
- `upgrade_snapshot` (warn): a pre-upgrade copy of the store (`muninn.sqlite.pre-upgrade-*`) was left because the installer could not delete it; it holds transcript text and `erase` cannot scrub it. Delete it.
- `release_leftovers` (warn): an old release has a remaining `.pruning-*` directory in `~/.local/lib/muninn`; the next `bin/muninn-install` removes it, or remove it manually.
- `poller_error` (warn): the last pass stopped with an exception; `detail` is its class name. The next successful poller pass clears it; a manual `ingest` does not. A persistent warning indicates recurring poller failures.
- `failed_sources` (warn): ordinary failed source read attempts during the last completed invocation, possibly including subsequently recovered sources. Repeated attempts count separately; preparation misses are excluded.
- `unreadable_files` (warn): the last completed invocation recorded that many failed transcript open attempts, possibly including subsequently recovered sources; check file permissions (macOS may need Full Disk Access for the poller).
- `reread` (info): `N of M` active sources are still to be re-read after an upgrade changed the classifier. The count normally reaches `0 of M`. If it remains above 0, inspect `failed_sources` and `stats` `last_pass.unreadable_files` (attempt totals, not distinct or unresolved source counts). It never fails the run.
- `review_backlog` (info), `review_policy`, `review_auth`, `review_config`, `review_abandoned` (warn): durable queue, policy and attempt-history counts/codes; no stored explanations.
- `review_process_deadline` (warn): recorded deadline failures or an unfinished attempt past its explicit policy deadline with an unended call; does not prove a live process or completed kill. `review_commit_delay` and `review_cancel_delay` report durable finalization separately.
- `review_coverage_gaps` and `review_capture_gaps` (warn): the same eligible set as stats (including admitted historical replay, excluding untouched activation archive) lacking receipts/current matching provenance, and new or uncertain previously admitted indexed records. Endpoint equality alone cannot prove interior content; missing/changed/erased same-request evidence stays a gap. Health reads no originals and preserves immutable receipts. Refusals/log loss never count as success; later explicit receipts do not fill preceding gaps.
- `review_quiescence` (info, null): witness presence only, containment unverified. `review_repair` (warn): repair baseline present. `review_erasure_intent` (info): journal presence, consistency unverified. Diagnostics never initialize/reset/delete these files. Missing DB/PID/free lock is not containment proof.
- `review_windows_repair` (warn, Windows): boot repair and durable baseline retirement are unqualified required-platform release blockers; fakes are not native qualification. Preserve all protocol evidence.

Real erase may interrupt the whole service. Read its `erasure_status`; `restart: not_attempted` requires checking and manually restarting if needed, never assuming it is stopped/restored. Retained archives prevent verified erasure. Original-source read budgets do not cap journal work, total I/O or command time; process deadlines do not cap DB commit/cancel delay.

Report failing checks. Claim a fix only after `doctor` is re-run and passes.
