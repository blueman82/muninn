# 0009: `bin/muninn-uninstall` removes muninn and keeps the data unless told otherwise

Status: Accepted, 2026-10-02. Decided by: owner (asked for an uninstall; the data default is the implementer's, inside that work).

**Context.** The installer could only undo an interrupted run, from a record that a successful upgrade deletes (ADR 0001, ADR
0002). There was no way to take muninn off a machine, and nothing recorded what to restore.

**Decision.** `bin/muninn-uninstall` is a wrapper for `bin/muninn-install --uninstall` (so Python is found in one place), which
runs `install/uninstall.py`:
- It stops the launchd job and removes its plist, removes our hook groups from Claude's `settings.json`, removes our
  sections from Codex's `config.toml` and our plugin cache, then removes the `muninn` link (only if it points into our lib
  dir) and the lib dir. It removes our entries by marker instead of restoring recorded before-values, because no record
  survives an upgrade. Each config edit goes through `configedit.edit_file` with the installer's checks, so nothing outside
  our own keys can change.
- The data dir holds the index and the cited knowledge ledger, which transcripts cannot rebuild. It is moved to
  `muninn-removed-<ts>` beside it, never deleted, unless `--purge-data` is given. Moving it also leaves the machine fresh,
  so `bin/muninn-install` works again (it refuses a half-installed machine).
- Everything that can refuse is checked before the first change, in a dry run too: both config edits are applied in memory and
  proved, and the name for the moved data must be free. A refusal leaves the machine as it was and exits 1 with one plain line.
- `--dry-run` says what would happen and writes nothing. Every action looks at launchd and the filesystem first, so a second
  run, or a run on a machine without muninn, is safe and says so, and a run after an interruption finishes the job.
- It adds no install mode: `--fresh` and `--upgrade` are unchanged.

**Consequences.**
- The owner runs `bin/muninn-uninstall`; agents run only `--dry-run`, as with `--check` in ADR 0008.
- A new place that stores muninn state must be added to `install/uninstall.py` and to `tests/test_uninstall.py` together.
- `muninn-install-<ts>` and `muninn-failed-<ts>` folders from earlier installs are left in place; they hold no transcript text
  (ADR 0004).
- A settings hook group is removed whole when any handler in it runs muninn, as the installer replaces whole groups. A hook
  the owner put in the same group as ours goes with it.
- The data dir is moved or deleted without taking `writer.lock`; it relies on the poller having been stopped first. A manual
  `muninn ingest` running at that moment would lose its directory.
