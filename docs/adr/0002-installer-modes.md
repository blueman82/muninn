# 0002: The installer has two modes, `--fresh` and `--upgrade`

Status: Accepted, 2026-10-01. Decided by: owner (one install path, nothing else to maintain).

**Context.** A new machine has no data directory, launchd job or release; an installed machine only needs to re-pin and
restart. Any other install path is code to maintain and test for no user.

**Decision.** `install/installer.py` runs `--fresh` (new machine) or `--upgrade` (re-pin a clean commit); exactly one is
required. `install/rollback.py` only undoes an interrupted run from its `rollback-record.json`.

**Consequences.** Everything the installer does is rehearsed in a temp HOME in `tests/test_installer.py`. `--fresh` has not
yet run on a second real machine. `classify.INJECTED_MARKERS` is unrelated: it flags injected memory blocks found inside
past transcripts.
