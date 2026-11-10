# 0004: Every log and report is allowlisted and holds no transcript text

## Status

Accepted

Date: 2026-10-01

## Context

Diagnostics (`poller.log`, `calls.jsonl`, `status.json`, `install.log`,
`install-record.json`, `doctor`, `stats`) must contain no transcript content when shared
from machines that cannot be examined directly.

## Decision

`poller.log` lines use the same field allowlist as `calls.jsonl`: a log code,
counts, and a Python error class name.
`poller.log` rotates at 1 MiB with two files.

The installer log records fixed step/command codes, timestamps and numeric exit status.
It does not keep step descriptions or external command stdout/stderr.
Install error records keep the error's class name, not its message.
The install record stores config key names, not values.

## Consequences

Diagnostics expose timing, counts and error classes, but omit potentially sensitive error messages.
Add each new log field to the allowlist in `muninn/obs_log.py` and test it in `tests/test_obs.py`.

## Installer preflight boundary (2026-10-08)

Logging starts only after the single read-only preflight succeeds.
Thus these conditions make no installer directory or log:

- A refused elevated Windows invocation.
- An unavailable Linux user bus.
- Provider config that does not satisfy safety checks.

Before append, the code checks log descriptors and their private parent.
It does not repair ownership or permissions that do not satisfy those checks.
Native log creation uses the private-file primitive.

Native metadata publication uses validated MoveFileExW with write-through.
Windows can refuse replacement while a delete-shared reader remains open.
Stop and status JSON can retry only native sharing/access errors 5 and 32 for at most one second.
Each try revalidates private objects and keeps their identities.
Validation errors and other native errors refuse immediately.

If a reader stays open longer than that limit, the previous complete data stays unchanged.
Closing the reader permits publication.
Power-loss durability remains unverified.
