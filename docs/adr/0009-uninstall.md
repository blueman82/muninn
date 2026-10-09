# 0009: Preserve data by default during uninstall

## Status

Accepted

Date: 2026-10-02

## Context

The installer could only undo an interrupted run, from a record that a successful
upgrade deletes (ADR 0001, ADR 0002). No explicit uninstall operation or durable
restoration record existed.

## Decision

`bin/muninn-uninstall` is a wrapper for `bin/muninn-install --uninstall` (so Python is
found in one place), which runs `install/uninstall.py`:

- It stops the launchd job and removes its plist, removes Muninn's hook groups from
  Claude's `settings.json`, removes Muninn's sections from Codex's `config.toml` and
  Muninn's plugin cache, then removes the `muninn` link (only if it points into Muninn's
  lib dir) and the lib dir. It removes Muninn's entries by marker instead of restoring
  recorded before-values, because no record survives an upgrade. Each config edit goes
  through `configedit.edit_file` with the installer's checks, so nothing outside
  Muninn's keys can change.
- The data dir holds the index and cited knowledge ledger. The ledger cannot be rebuilt
  from transcripts. It is moved to `muninn-removed-<ts>` beside it, never deleted,
  unless `--purge-data` is given. Moving it also leaves the machine fresh, so
  `bin/muninn-install` works again (it refuses an incomplete installation).
- Preflight refusal checks run before the first change, including during a dry run: both
  config edits are applied in memory and proved, and the name for the moved data must be
  free. A refusal leaves the machine as it was and exits 1 with one diagnostic line.
- `--dry-run` previews the operation and writes nothing. Actions inspect launchd and the
  filesystem first. Repeated runs and runs on an uninstalled machine report the existing
  state; interrupted runs can resume.
- It adds no install mode: `--fresh` and `--upgrade` are unchanged.

## Consequences

- The owner runs `bin/muninn-uninstall`; agents run only `--dry-run`, as with `--check`
  in ADR 0008.
- A new place that stores muninn state must be added to `install/uninstall.py` and to
  `tests/test_uninstall.py` together.
- `muninn-install-<ts>` and `muninn-failed-<ts>` folders from earlier installs are left
  in place; they hold no transcript text (ADR 0004).
- A settings hook group is removed whole when any handler in it runs muninn, as the
  installer replaces whole groups. Any unrelated handler in the same group is also
  removed.
- The data dir is moved or deleted without taking `writer.lock`; it relies on the poller
  having been stopped first. A concurrent manual `muninn ingest` can lose access to its
  data directory.
