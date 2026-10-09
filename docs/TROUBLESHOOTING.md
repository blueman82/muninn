# Troubleshooting

Run `muninn --pretty doctor`. Each check has `ok`, a `level` (`error` makes the exit
code 1; `warn` and `info` do not) and a `detail`. When asking for help, share `doctor`,
`stats`, `install.log` and `install-record.json`. None of them holds transcript text.

| Symptom | Likely cause | What to do |
|---|---|---|
| `muninn: no Python 3.13+ found` | no interpreter on PATH or at the installer's link | set `MUNINN_PYTHON=/path/to/python3.13`, or re-run the installer under the right Python |
| `poller: stale` in every answer, `heartbeat` fails | the launchd job is down or stuck | `launchctl print gui/$(id -u)/com.muninn`; read `poller.log`; `launchctl kickstart -k gui/$(id -u)/com.muninn` |
| `poller: stale` while the poller runs | one source is taking longer than 3 intervals (the alive stamp is written between sources, not inside one), or the poller is on a release older than the alive stamp | wait for the pass to end (`muninn stats` shows `reread.pending` falling and `last_pass.alive_age_s` while it runs, and an `alive_age_s` that keeps growing means the poller died mid-pass and was not restarted; `poller.log` gets a line only when a pass finishes); a pass that keeps failing or finding the lock busy never stamps, so it reports `stale` |
| `launchd_job` fails | job not loaded | re-run `bin/muninn-install` (it upgrades an installed machine) |
| search finds little | few sources indexed | `muninn --pretty stats`: check `sources`, `events_by_provider` (a provider at 0 means its root was not found) and `last_pass.unreadable_files` (files the poller could not open) and `last_pass.skipped_files`; `muninn doctor` shows `roots_present` |
| `db_free_space` warns | many deletes left free pages | `muninn compact` (needs about one database's worth of free disk) |
| `aside_files` warns | a `rebuild` set aside an unreadable store, `muninn.sqlite.unreadable-<ts>`; `erase` cannot scrub it, so erased text may survive there | salvage what you need, then run the command that `muninn erase` prints in `aside_remove` (`rm --` on macOS/Linux; PowerShell `Remove-Item -LiteralPath` with a literal path array on Windows) (it is not deleted automatically) |
| `upgrade_snapshot` warns | an `--upgrade` that migrated the store left `muninn.sqlite.pre-upgrade-<sha>` (a full copy of the index and ledger) because it could not delete it; `erase` cannot scrub it | run the printed `aside_remove` command from `muninn erase` (`rm --` on macOS/Linux; PowerShell `Remove-Item -LiteralPath` with a literal path array on Windows); the copy is only needed while an upgrade can still roll back |
| exit 4, `error` `tombstone_key`; `doctor` `tombstone_key` fails (`missing` or `damaged`) | `tombstone.key` is missing or damaged while keyed tombstones exist; ingest, rebuild and erase refuse to run | restore `tombstone.key` from a backup. Do not recreate it: a new key stops the existing tombstones matching, so erased content could come back |
| `release_leftovers` warns | an upgrade's sweep could not delete an old `.pruning-<ts>-<sha>` release directory in `~/.local/lib/muninn` | the next `bin/muninn-install` retries the sweep; or delete the named directory by hand |
| `db_size` warns | database over 2 GiB | `muninn compact`; consider `muninn erase` of old sessions |
| exit 3, `busy` | another writer holds the lock (poller pass, compact, erase) | retry in a few seconds |
| `store_unavailable` with `schema vN, need v3` (hooks say `memory unavailable`) | the file is schema v1/v2 and no writer has migrated it yet, or a newer Muninn wrote it (`doctor` `store_readable`: false) | run `muninn doctor`; for v1/v2, run a writing command (`muninn ingest`) to migrate it; for a schema newer than v3, upgrade Muninn. Schema migration is one-way; there is no downgrade |
| exit 4, `hot_journal` / `store_unavailable` | a writer crashed mid-transaction, or a schema mismatch | run any writing command (`muninn ingest`) to roll the journal back; `doctor` shows `unowned_journal` |
| `unexpected_files` fails | a stray file in the data dir | move it out; `recall.off` and the logs are expected |
| hooks print nothing | `MUNINN_HOOK_DISABLE=1`, or `recall.off` exists in the data dir | unset it, or delete `recall.off` |
| Codex shows no memory | plugin hooks not trusted | start Codex, run `/hooks`, trust the two hooks |
| Cursor history is missing after compaction | optional native hook absent or disabled, database path unavailable, invalid `message_count`, missing/mismatched conversation identity, invalid/ambiguous workspace roots, oversized conversation, or refresh failure | check `~/.cursor/hooks.json` against [Cursor setup](QUICKSTART.md#cursor-history); outside macOS set `MUNINN_CURSOR_DB` in the Cursor environment. Successful refresh and a missing database return `{}`; invalid payload identity/count/workspace or import failure reports a `user_message`. Confirm Cursor supplies `conversation_id` and native absolute `workspace_roots`; no latest-row fallback is used. Use `muninn ingest --full` for the complete import; `recall.off` controls later prompt recall, not the refresh. The [90-second benchmark](CURSOR-PRECOMPACT-BENCHMARK.md) does not guarantee completion on every host |
| installer failed | see `install.log` and `install-record.json` (`outcome`, `failed`) | it already rolled back, and put the pre-upgrade store copy back if the new release had migrated the store (`snapshot.state` `restored`); fix the cause and re-run |
| uninstall requested | removal operation | `bin/muninn-uninstall --dry-run`, then `bin/muninn-uninstall`; the data moves to `~/.local/share/muninn-removed-<ts>` (`--purge-data` deletes it) |
| `bin/muninn-install` says half-installed after an uninstall | an uninstall that was interrupted left the data dir or the release link | run `bin/muninn-uninstall` again; it removes whatever is left |

## Logs

- `poller.log`: one JSON line per event (`start`, `pass` when files changed or failed,
  hourly `idle`, `error` with an exception class, `heartbeat_failed` with the class of
  the error when the alive stamp could not be written (once per pass; the poller will
  then go `stale`), `crash` with the class of an exception that ended the poller outside
  a pass (it exits 1 and launchd restarts it), `stop`, `migrated`, `migrate_raced` and
  `migrate_failed` for schema upgrades (`from_v`, `to_v`, `rows`, `ms`, and `exc` on a
  failure), and `quiet_failed` with the class of the error when the poller could not
  redirect launchd's output away from the log). Rotates at 1 MiB to `poller.log.1`.
- `calls.jsonl`: one line per CLI call, IDs and counts only; rotates likewise.
- `status.json`: the last pass and heartbeat.
