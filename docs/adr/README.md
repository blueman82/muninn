# Architecture decision records

Each record documents its status, context, decision, and consequences.
Recorded dates identify acceptance. Each exception or deferred decision has an explicit status.

| # | Decision |
|---|---|
| [0001](0001-one-pinned-release.md) | Exactly one pinned release. Upgrade in place. |
| [0002](0002-installer-modes.md) | The installer has two modes, `--fresh` and `--upgrade` |
| [0003](0003-interpreter-resolution.md) | `bin/muninn` finds its interpreter at run time |
| [0004](0004-content-free-logs.md) | Every log and report is allowlisted and holds no transcript text |
| [0005](0005-no-hash-chained-call-log.md) | Defer hash-chained call logging |
| [0006](0006-mechanical-standards.md) | Enforce engineering standards with automated gates |
| [0007](0007-recall-off-by-default.md) | Per-prompt recall starts off on a fresh install |
| [0008](0008-install-wrapper.md) | Select the install mode automatically and provide a read-only preview |
| [0009](0009-uninstall.md) | Preserve data by default during uninstall |
| [0010](0010-chatgpt-handoff-sessions.md) | Classify Codex ChatGPT-handoff sessions as primary |
| [0011](0011-one-way-schema-migration-with-snapshot.md) | Snapshot the store before a one-way schema upgrade |
| [0012](0012-keyed-content-tombstones.md) | Use keyed HMAC content tombstones with a durable secret. Separate design approval not recorded. |
| [0013](0013-cursor-imports.md) | Import Cursor history during full ingest |
| [0015](0015-default-learning-policy.md) | Install the repository learning policy by default |
| [0016](0016-confirm-legacy-macos-recovery.md) | Ask before repairing a matching older macOS record without a disk ID |
| [0017](0017-learning-scope-and-recovery.md) | Choose learning scope and recover bounded review failures. Narrow correction amendment PROPOSED. Owner approval pending. |
| [0018](0018-conversational-memory-decisions.md) | PROPOSED: bind owner decisions to exact memory candidates. Owner approval pending. |
