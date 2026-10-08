# 0004: Every log and report is allowlisted and holds no transcript text

Status: Accepted, 2026-10-01. Decided by: implementer, within the owner's items on poller and installer logging.

**Context.** A colleague's machine cannot be inspected, so the files they share for support (`poller.log`, `calls.jsonl`,
`status.json`, `install.log`, `install-record.json`, `doctor`, `stats`) must be safe to send.

**Decision.** `poller.log` lines go through the same field allowlist as `calls.jsonl` (an event code, counts, an exception
class name) and rotate at 1 MiB, two files. The installer log records fixed progress/command codes, timestamps and numeric exit status.
Neither progress text nor external command stdout/stderr is persisted. Failed install records retain the exception class,
not its message. The install record stores config key names, never values.

**Consequences.** Support gets timing, counts and error classes, not what a failure was about. Adding a log field means
adding it to the allowlist in `muninn/obs_log.py` and covering it in `tests/test_obs.py`.


## Installer preflight boundary (2026-10-08)

Logging is attached only after the single read-only preflight succeeds. Refused
elevated Windows invocation, an unavailable Linux user bus, and unsafe provider
config therefore create no installer directory or log. Existing log descriptors
and their private parent are checked before append, without repairing unsafe
ownership or permissions. Native log creation uses the private-file primitive.
