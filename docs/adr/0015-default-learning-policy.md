# 0015: Install the repository learning policy by default

## Status

Accepted.
Default limits and refresh rules are updated by [ADR 0017](0017-learning-scope-and-recovery.md).

Date: 2026-10-25

## Context

Before this change, the installer needed `--review-policy PATH` to set a
learning policy. Without one, SessionEnd and PreCompact could not accept
learning requests. The policy also needs host-specific Codex capability data.
For that reason, the repository cannot ship one ready-to-run local policy.

## Decision

The repository ships an editable default profile at
`tools/default_codex_policy.json`. An install that uses the current default
home selects this profile when the store has no policy and the host meets the
Codex runner requirements. An upgrade also selects it for a store with no
policy.

Before installation changes begin, Muninn freezes the selected policy and
local paths. It creates private capability files for the host after it has
prepared the store and provider configuration. It then commits the policy,
admission and source boundaries together.

The default runner requires Apple silicon macOS, a signed Codex CLI on `PATH`,
and an existing canonical Codex sign-in directory. If a check fails, the store
stays unconfigured. The default allows project scope, one attempt, one
concurrent request and at most two rounds. It also sets input, output, context
and time limits for each request. It has no total call limit, cost limit or
expiry.

Upgrades keep custom policies and disabled admission unchanged. Muninn can
refresh a policy marked as the repository default only when admission is
enabled and the learning-request and knowledge tables are empty. The new
profile must have a later approval time.

Refresh keeps the host and runner bindings, prompt, budget identity, admission
and activation time. It can lower input, round, output, context and deadline
limits. Input can increase only from v1 to v2, and cannot exceed 1 MiB. Refresh
cannot change attempts, concurrency, hook wait, scope, model, reasoning or
budget identity. Muninn stores the policy's source so it does not refresh a
custom policy as a repository default. Recovery uses the exact profile frozen
before installation changes begin.

## Consequences

- A supported fresh install has automatic learning available without a
  `--review-policy` argument.
- `recall.off`, `recall.advisory` and `recall.on` remain prompt-recall choices.
  They do not gate SessionEnd or PreCompact learning.
- Hook trust remains a separate provider permission.
- Users can change the repository profile before first activation, within the
  fixed safety bounds. The reference docs list the allowed fields and explain
  when Muninn can refresh an unused repository default.
- Existing custom settings and disabled admission survive upgrades unchanged.
- An unused enabled repository default can follow approved profile updates
  within its fixed safety bounds. A learning request or knowledge entry stops
  that refresh.
- The default is ongoing and has no aggregate quota or expiry. The per-request
  byte, round, deadline, attempt, concurrency and scope limits still apply.
