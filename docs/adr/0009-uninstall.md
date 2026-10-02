# 0009: `bin/muninn-uninstall` removes muninn and keeps the data unless told otherwise

Status: Accepted, 2026-10-02. Decided by: owner (asked for an uninstall; the data default is the implementer's, inside that work).

**Context.** The installer could only undo an interrupted run, from a record that a successful upgrade deletes (ADR 0001, ADR
0002). There was no way to take muninn off a machine, and nothing recorded what to restore.

**Decision.** `bin/muninn-uninstall` is a three-line wrapper for `bin/muninn-install --uninstall`, which runs
`install/uninstall.py`:
- It stops the launchd job and removes its plist, removes our hook groups from Claude's `settings.json`, removes our
  sections from Codex's `config.toml` and our plugin cache, then removes the `muninn` link (only if it points into our lib
  dir) and the lib dir. It removes our entries by marker instead of restoring recorded before-values, because no record
  survives an upgrade. Each config edit goes through `configedit.edit_file` with the installer's checks, so nothing outside
  our own keys can change.
- The data dir holds the index and the cited knowledge ledger, which transcripts cannot rebuild. It is moved to
  `muninn-removed-<ts>` beside it, never deleted, unless `--purge-data` is given. Moving it also leaves the machine fresh,
  so `bin/muninn-install` works again (it refuses a half-installed machine).
- `--dry-run` says what would happen and writes nothing. Every action looks at launchd and the filesystem first, so a second
  run, or a run on a machine without muninn, is safe and says so.
- It adds no install mode: `--fresh` and `--upgrade` are unchanged.

**Consequences.**
- The owner runs `bin/muninn-uninstall`; agents run only `--dry-run`, as with `--check` in ADR 0008.
- A new place that stores muninn state must be added to `install/uninstall.py` and to `tests/test_uninstall.py` together.
- `muninn-install-<ts>` rollback folders are left in place; they hold no transcript text (ADR 0004).
