# 0006: The trial harness is deleted

Status: Accepted, 2026-10-01. Decided by: owner ("trial is over get it gone").

**Context.** `trial_harness/` drove the frozen old-versus-new evaluation. It hardcoded this machine's paths, including the
deleted old build tree. The trial ended inconclusive (28 valid pairs, McNemar p = 0.34).

**Decision.** Delete `trial_harness/` and `tests/trial_harness/`. They remain in git history.

**Consequences.** Re-running the trial means restoring them from history and repointing the paths. The four retrieval fixes
the trial's root-cause report called for are already in the code; their effect is unmeasured.
