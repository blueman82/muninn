# 0008: `bin/pctx-install` picks the install mode and previews without writing

Status: Accepted, 2026-10-02. Decided by: owner (one command to install or upgrade, with no flags to remember).

**Context.** ADR 0002 gives the installer two modes, `--fresh` and `--upgrade`, and the caller has to pick the right one and
pass `--repo` and `--sha`. A wrong choice is easy to make and an upgrade deletes the previous release (ADR 0001).

**Decision.** `bin/pctx-install` is a thin sh wrapper over `install/installer.py`:
- Bare, it installs this checkout's HEAD: `--fresh` when there is no current release and no data directory, `--upgrade` when
  both exist. If only one of them exists it refuses and says so, because the machine is half-installed.
- `--check` runs the installer's dry run and writes nothing, not even the install log. It ends with the command to run.
- `--status` compares the installed commit with this checkout's HEAD and runs no installer code.
- `--help` describes the three forms above.
- Any other arguments go straight to `install.installer`, so `--repo`, `--sha`, `--fresh`, `--upgrade` and `--dry-run` keep
  working unchanged. The wrapper adds no mode the installer lacks.
- A real run prints one summary line (commit, mode, poller pid, log path). The step-by-step commands stay in `install.log`.

**Consequences.**
- The owner runs `bin/pctx-install`; agents run only `--check` and `--status`.
- The wrapper's mode detection reads the `current` link and the data directory, so a layout change in `install/` must change
  the wrapper and `tests/test_install_wrapper.py` together.
