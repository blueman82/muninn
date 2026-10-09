# 0003: `bin/muninn` finds its interpreter at run time

## Status

Accepted

Date: 2026-10-01

## Context

`bin/muninn` ran `/opt/homebrew/bin/python3.13`, which does not exist on every machine.
launchd gives the job only `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, so a plain PATH lookup
would also fail there.

## Decision

Resolve the interpreter in this order: `$MUNINN_PYTHON`; the link
`~/.local/lib/muninn/python`, which the installer points at the Python it ran under;
then the first of `python3.13`, `python3.14`, `python3` on PATH that is 3.13 or newer.
If none qualifies, print how to set `MUNINN_PYTHON` and exit 127.

## Consequences

The minimum supported version is 3.13; testing covers that version. The link is resolved
once, at install time, and recorded in `install-record.json`. A Homebrew upgrade that
moves the interpreter invalidates the link until the next `--upgrade`; the PATH fallback
then covers interactive use but not the launchd job.

## Native Windows and Linux extension (2026-10-08)

Windows records the interpreter in the exact private `lib/selection.json` manifest
beside one pinned release. The stable runtime `muninn.cmd` invokes `muninn.ps1`; that
launcher checks selection, private ancestry and held handles before importing selected
code. Interpreter precedence remains explicit `MUNINN_PYTHON`, recorded Python, then the
first trusted Python 3.13+ on PATH. A missing old path permits fallback; an unsafe
present candidate refuses before execution. No machine-specific interpreter path is
embedded in the wrapper.

Managed services use the interpreter validated by the installer: the Windows Task
Scheduler action invokes the same stable PowerShell launcher with the recorded
interpreter; Linux invokes that interpreter with a pinned bootstrap that validates
private state before imports. Upgrade repairs the recorded interpreter/action. The
action definition alone does not establish later integrity of Python dependencies.

Native Windows interpreter fields require a fixed local `.exe` path, checked before the
version probe. Batch and script candidates are refused before any execution, preventing
recursive wrapper probes. A missing recorded `.exe` continues to use the validated
runtime fallback.

The Linux bootstrap records its validated loaded directory SHA before imports. The
writer alone publishes `writer_install_sha` with its PID; a manual ingest can update the
ordinary selected-release heartbeat without relabeling the writer. `muninn doctor`
requires the exact owned unit, no drop-ins, the actual native interpreter and arguments,
and this loaded SHA. Rollback renders the service again from the restored release and
interpreter selections before starting it.
