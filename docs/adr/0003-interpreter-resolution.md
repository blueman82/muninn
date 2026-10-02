# 0003: `bin/muninn` finds its interpreter at run time

Status: Accepted, 2026-10-01. Decided by: implementer, within the owner's item "drop the hardcoded python path".

**Context.** `bin/muninn` ran `/opt/homebrew/bin/python3.13`, which does not exist on every machine. launchd gives the job
only `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, so a plain PATH lookup would also fail there.

**Decision.** Use, in order: `$MUNINN_PYTHON`; the link `~/.local/lib/muninn/python`, which the installer points
at the Python it ran under; then the first of `python3.13`, `python3.14`, `python3` on PATH that is 3.13 or newer. If none
qualifies, print how to set `MUNINN_PYTHON` and exit 127.

**Consequences.** The version floor is 3.13, the only version the code is tested on. The link is resolved once, at install
time, and recorded in `install-record.json`. A Homebrew upgrade that moves the Python breaks the link until the next
`--upgrade`; the PATH fallback then covers interactive use but not the launchd job.
