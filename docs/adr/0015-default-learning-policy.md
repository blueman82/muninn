# 0015: Install the repository learning policy by default

## Status

Accepted

Date: 2026-10-25

## Context

The installer previously completed without a review policy unless the owner
passed `--review-policy PATH`. SessionEnd and PreCompact then could not admit a
learning request. The policy also contains host-specific Codex capability data,
so the repository cannot ship one ready-to-run local policy file.

## Decision

The repository ships an editable default profile in
`tools/default_codex_policy.json`. A normal first install, or an upgrade of an
unconfigured store, selects that profile when the host meets the native Codex
runner requirements. Muninn freezes the selected policy and local paths before
installation changes. It creates private host-bound capability files only
after the store and provider configuration are ready, then commits policy,
admission and source boundaries together.

The default runner is limited to Apple silicon macOS, a signed Codex CLI on
`PATH`, and an existing canonical Codex sign-in directory. If those checks fail,
the store remains unconfigured. The default uses project scope, one attempt,
one concurrent request, two rounds, bounded input/output/context and a deadline.
The ongoing budget has no aggregate call limit, cost limit or expiry.

Upgrades preserve custom policies and disabled admission. A policy tagged as
the repository default can refresh only while admission is enabled and both
learning-request and knowledge tables are empty. The new profile must record a
later approval time. Refresh preserves host and runner bindings, prompt,
budget identity, admission and activation time. It can reduce input, rounds,
output, context and deadline; input may increase only from v1 to v2, up to
1 MiB. It cannot change attempts, concurrency, hook wait, scope, model,
reasoning or budget identity. Default provenance is stored with the policy so
custom policies are never treated as refreshable defaults. Recovery uses the
exact profile frozen before installation changes begin.

## Consequences

- A supported fresh install has automatic learning available without a
  `--review-policy` argument.
- `recall.off`, `recall.advisory` and `recall.on` remain prompt-recall choices.
  They do not gate SessionEnd or PreCompact learning.
- Hook trust remains a separate provider permission.
- Users can change the repository profile before first activation, within the
  model's fixed safety bounds. The reference docs describe supported fields and
  when an unused repository default can refresh.
- Existing custom settings and disabled admission survive upgrades unchanged.
- An unused enabled repository default follows approved profile updates within
  its fixed safety bounds; any learning request or knowledge entry freezes it.
- The default is ongoing and has no aggregate quota or expiry. The per-request
  byte, round, deadline, attempt, concurrency and scope limits still apply.
