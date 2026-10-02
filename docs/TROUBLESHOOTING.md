# Troubleshooting

Start with `muninn --pretty doctor`. Each check has `ok`, a `level`
(`error` makes the exit code 1; `warn` and `info` do not) and a `detail`.
When asking for help, share `doctor`, `stats`, `install.log` and
`install-record.json`. None of them holds transcript text.

| Symptom | Likely cause | What to do |
|---|---|---|
| `muninn: no Python 3.13+ found` | no interpreter on PATH or at the installer's link | set `MUNINN_PYTHON=/path/to/python3.13`, or re-run the installer under the right Python |
| `poller: stale` in every answer, `heartbeat` fails | the launchd job is down or stuck | `launchctl print gui/$(id -u)/com.muninn`; read `poller.log`; `launchctl kickstart -k gui/$(id -u)/com.muninn` |
| `poller: stale` while the poller runs | the pass is long (for example the one-time re-read after an upgrade that changes the classifier) and the poller is on a release older than the heartbeat-during-pass fix | wait for the pass to end, or check `poller.log`; a current poller stays `ok` through a long pass, so `stale` there means it is stuck or stopped |
| `launchd_job` fails | job not loaded | re-run `bin/muninn-install` (it upgrades an installed machine) |
| search finds little | few sources indexed | `muninn --pretty stats`: check `sources`, `events_by_provider` (a provider at 0 means its root was not found) and `last_pass.skipped_files`; `muninn doctor` shows `roots_present` |
| `db_free_space` warns | many deletes left free pages | `muninn compact` (needs about one database's worth of free disk) |
| `db_size` warns | database over 2 GiB | `muninn compact`; consider `muninn erase` of old sessions |
| exit 3, `busy` | another writer holds the lock (poller pass, compact, erase) | retry in a few seconds |
| exit 4, `hot_journal` / `store_unavailable` | a writer crashed mid-transaction, or a schema mismatch | run any writing command (`muninn ingest`) to roll the journal back; `doctor` shows `unowned_journal` |
| `unexpected_files` fails | a stray file in the data dir | move it out; `recall.off` and the logs are expected |
| hooks print nothing | `MUNINN_HOOK_DISABLE=1`, or `recall.off` exists in the data dir | unset it, or delete `recall.off` |
| Codex shows no memory | plugin hooks not trusted | start Codex, run `/hooks`, trust the two hooks |
| installer failed | see `install.log` and `install-record.json` (`outcome`, `failed`) | it already rolled back; fix the cause and re-run |
| want muninn gone | not a fault | `bin/muninn-uninstall --dry-run`, then `bin/muninn-uninstall`; the data moves to `~/.local/share/muninn-removed-<ts>` (`--purge-data` deletes it) |
| `bin/muninn-install` says half-installed after an uninstall | an uninstall that was interrupted left the data dir or the release link | run `bin/muninn-uninstall` again; it removes whatever is left |

## Logs

- `poller.log`: one JSON line per event (`start`, `pass` when files changed or
  failed, hourly `idle`, `error` with an exception class, `stop`). Rotates at
  1 MiB to `poller.log.1`.
- `calls.jsonl`: one line per CLI call, IDs and counts only; rotates likewise.
- `status.json`: the last pass and heartbeat.
