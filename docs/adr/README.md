# Architecture decision records

Each record documents its status, context, decision, and effects.
Recorded dates identify `acceptance`.
Each `exception` or deferred decision has an explicit status.

| # | Decision |
|---|---|
| [0001](0001-one-pinned-release.md) | Only one pinned release. Upgrade at the current path. |
| [0002](0002-installer-modes.md) | The installer has two modes, `--fresh` and `--upgrade` |
| [0003](0003-interpreter-resolution.md) | `bin/muninn` finds its interpreter at run time |
| [0004](0004-content-free-logs.md) | Each log and report is allowlisted and holds no transcript text |
| [0005](0005-no-hash-chained-call-log.md) | Defer hash-chained `call` logging |
| [0006](0006-mechanical-standards.md) | Apply engineering standards with automated gates |
| [0007](0007-recall-off-by-default.md) | Per-prompt recall starts off on a new install |
| [0008](0008-install-wrapper.md) | Select the install mode automatically and give a read-only preview |
| [0009](0009-uninstall.md) | Keep data by default during uninstall |
| [0010](0010-chatgpt-handoff-sessions.md) | Classify Codex ChatGPT-handoff sessions as primary |
| [0011](0011-one-way-schema-migration-with-snapshot.md) | Snapshot the store before a one-way schema upgrade |
| [0012](0012-keyed-content-tombstones.md) | Use keyed HMAC content tombstones with a durable secret. This design has no recorded approval given independently. |
| [0013](0013-cursor-imports.md) | Import Cursor history during full `ingest` |
| [0015](0015-default-learning-policy.md) | Install the repository learning policy by default. Replaced by ADR 0026. |
| [0016](0016-confirm-legacy-macos-recovery.md) | Get approval before repairing a matching older macOS record without a disk ID. Replaced by ADR 0026. |
| [0017](0017-learning-scope-and-recovery.md) | Select learning scope and recover bounded `review` errors. Replaced by ADR 0026. |
| [0018](0018-conversational-memory-decisions.md) | Connect owner decisions to unchanged memory candidates. Accepted 2026-10-29, changed by ADR 0026 (owner-decision rules only). |
| [0019](0019-current-codex-interface.md) | Follow the current trusted Codex interface with one initial checked rule. Replaced by ADR 0026. |
| [0020](0020-owner-unknown-call-acknowledgement.md) | Accept one named historical uncertainty while preserving its state, charge and generation fence. Replaced by ADR 0026. |
| [0022](0022-provenance-trust.md) | Trust owner lines by provenance, not wording. The proof is the full cited owner message, plus the assistant reply directly before an agreement. Accepted 2026-11-15, revised 2026-11-16. |
| [0023](0023-no-read-limit-before-the-window.md) | Lines before the window start do not count against the source read limits. Accepted 2026-11-16, changed by ADR 0026. |
| [0024](0024-leave-unfinished-start.md) | Leave an unfinished first-policy upgrade start with one record mark. Replaced by ADR 0026. |
| [0025](0025-own-budget-for-each-window.md) | Each window gets its own read budget. Replaced by ADR 0026. |
| [0026](0026-simple-learning.md) | Simple learning: macOS only, `muninn learn on/off`, one settings file, one learner, one constant policy, one sentence per failure. Proposed. ADR 0021 was never used. |
