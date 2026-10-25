# 0008: Select the install mode automatically and provide a read-only preview

## Status

Accepted

Date: 2026-10-02

## Context

ADR 0002 gives the installer two modes, `--fresh` and `--upgrade`, and the caller has to
pick the right one and pass `--repo` and `--sha`. Explicit mode selection is
error-prone, and a successful upgrade deletes the previous release (ADR 0001).

## Decision

`bin/muninn-install` is a thin sh wrapper over `install/installer.py`:

- Without arguments, it installs this checkout's HEAD: `--fresh` when there is no
  current release and no data directory, `--upgrade` when both exist. If only one of
  them exists it refuses with an incomplete-installation diagnostic.
- If the selected Python cannot import the pinned Pydantic version, the wrapper asks
  before creating the checkout's `.venv` and installing the hashed runtime lock. Declining
  runs no installer.
- On macOS/Linux first install, the wrapper also offers to enable this checkout's Git hooks. Accepting
  installs `requirements-dev.txt` in `.venv` and sets local `core.hooksPath` to `.githooks`;
  an existing custom hooks path is left unchanged.
- `--check` runs the installer's dry run without changing installed state or writing an
  install log. Accepted runtime or Git-hook setup may create `.venv` and change local Git
  configuration. It ends with the command to run.
- `--status` compares the installed commit with this checkout's HEAD and runs no
  installer code.
- `--help` describes the three forms above.
- Any other arguments go straight to `install.installer`, so `--repo`, `--sha`,
  `--fresh`, `--upgrade` and `--dry-run` keep working unchanged. The wrapper introduces
  no additional installer mode.
- A real run prints one summary line (commit, mode, poller pid, log path). The fixed
  progress codes and command exit statuses stay in `install.log`.

## Consequences

- The owner runs `bin/muninn-install`; agents run only `--check` and `--status`.
- The wrapper's mode detection reads the `current` link and the data directory, so a
  layout change in `install/` must change the wrapper and
  `tests/test_install_wrapper.py` together.

## Native maintenance boundary (2026-10-08)

Windows source checkouts provide dedicated `.cmd` and `.ps1` install/uninstall wrappers.
They forward through the runtime launcher's validation using a reserved first-argument
installer entry marker and preserve arguments and exit codes. Only the runtime
`.cmd`/`.ps1` pair is installed in the stable private bin directory. Maintenance
commands remain source-checkout commands, like the POSIX installer: a pinned archive
contains no checkout Git metadata, and running destructive maintenance inside its held
release guards would prevent safe pruning/removal. Release guards remain held throughout
runtime use.

Fresh and upgrade are the only modes. Explicit synthetic Windows homes resolve their own
AppData/Local/Muninn tree; inherited LOCALAPPDATA is used only when `--home` is omitted.
Runtime MUNINN_HOME remains authoritative. `--check` writes no state, config, log,
selection or task/unit definition. Real CLI uninstall retains data by default; agents
invoke only `--dry-run`.

Linux requires a working per-user systemd manager/user bus before mutation; Windows
requires an ordinary interactive user token and an owned per-user Task Scheduler
definition with LeastPrivilege. The installer does not configure linger, administrator
services, passwords or elevated tasks. Stop disables automatic launches before
requesting graceful exit. Windows binds the actual writer generation and launcher
creation identities to the owned action and running task engine (paths compared by
resolved identity); both processes and all task instances must exit. Linux uses
unbounded systemd stop with no SIGKILL fallback. A bounded installer wait that expires
refuses restoration/config undo/pruning and retains private state.

Native upgrades pause before snapshot/publication. SQLite snapshots and selection
publication use atomic replacement without deleting the live file first; uncertain
copies remain private. The durable HMAC tombstone key is never recreated over existing
private history and is unchanged by snapshot restore.
