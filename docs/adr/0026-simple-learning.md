# 0026: Simple learning

## Status

Accepted 2026-11-24.
It replaces ADRs 0015, 0016, 0017, 0019, 0020, 0024 and 0025.
It changes ADR 0018 (owner decisions only) and ADR 0023.

## Context

Muninn has one owner on one Mac. The earlier learning design added a default
policy per release, legacy recovery, a Codex interface rule, owner
acknowledgement of unknown calls, an unfinished-start mark and many checks
around money, pauses and process watching. Most of that guarded cases that
one owner on one machine does not meet. It cost much code and many documents.

## Decision

- **macOS only.** Muninn runs only on macOS.
- **Switch.** `muninn learn on` and `muninn learn off` set `enabled` in
  `learning.json` in the Muninn home.
- **Settings file.** `learning.json` holds `enabled`, `provider` (`claude`,
  which runs the Claude CLI, or `codex`), `model` and `effort`. The default
  is the Claude CLI with model `sonnet` and effort `low`. Learning is off
  until the owner turns it on.
- **One learner.** The poller (`serve`) runs one learner. There is no
  second runner and no per-release default policy.
- **One constant policy.** The policy comes from the settings and from
  constants in the source. It is not installed, renewed or approved
  separately, and no install step checks it.
- **Plain writer lock.** The learner uses the ordinary exclusive
  `writer.lock`. It does not pause the poller, watch processes or check
  money.
- **One sentence per failure.** Each failure has one code and one plain
  sentence. A failed request is not repaired by hand. The next pass tries
  again. A call that hangs ends at a time limit.
- **Read budget for each window.** Each live or catch-up window gets its
  own read budget. Lines before the window start, the line-1 identity read
  and the boundary read charge nothing (ADR 0023, with the identity and
  boundary rule that ADR 0025 proposed). A window that does not fit is cut
  or skipped with one log record.
- **Owner decisions.** ADR 0018 stays for the candidate, decision, proof
  and apply rules. Its policy, pause, host and maintenance-pause rules go.
- **No approval layers.** There is no approval digest for a policy, no
  leave-unfinished mark and no owner acknowledgement of an unknown call.

## Schema

The schema stays at its current version. There is no migration. Tables that
the learner no longer uses stay in existing store files and are left alone.
Failed requests stay in the store. Nothing is removed from an existing file.

## Consequences

- A change to learning policy needs a new ADR.
- An older store opens as before. A new store has only the tables that kept
  code uses.
- An upgrade that completes erases the previous release (ADR 0001), so there
  is no way back to it without the copy of ADR 0011.
- A learning failure shows one sentence and does not stop search or recall.
