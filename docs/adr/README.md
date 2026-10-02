# Architecture decision records

One short record per decision. Status is `Accepted` unless noted. "Decided by" says whether the owner chose it or the
implementer chose it inside work the owner ordered.

| # | Decision |
|---|---|
| [0001](0001-one-pinned-release.md) | Exactly one pinned release; upgrade in place |
| [0002](0002-installer-modes.md) | The installer has two modes, `--fresh` and `--upgrade` |
| [0003](0003-interpreter-resolution.md) | `bin/pctx` finds its interpreter at run time; no hardcoded path |
| [0004](0004-content-free-logs.md) | Every log and report is allowlisted and holds no transcript text |
| [0005](0005-no-hash-chained-call-log.md) | No hash-chained call log for now |
| [0006](0006-mechanical-standards.md) | Engineering standards are enforced by a gate, not by instructions |
| [0007](0007-recall-off-by-default.md) | Per-prompt recall starts off on a fresh install |
| [0008](0008-install-wrapper.md) | `bin/pctx-install` picks the install mode and previews without writing |
