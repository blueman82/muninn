---
name: muninn-compact
description: Reclaim muninn database space with `muninn compact`. Use when `muninn doctor` warns db_free_space or `muninn stats` shows a high `db_space.free_ratio`, or when the user asks to shrink or vacuum the muninn database.
---

# muninn compact

    muninn compact --pretty

Runs VACUUM under the writer lock and reports `compact.bytes_before` / `bytes_after`.

- Requires free disk space approximately equal to the database size. Do not bypass a refusal for insufficient space.
- Blocks the poller and other writers. Exit code 3 means the writer lock is held; wait and retry.
- Check `muninn stats` first. Compact when free pages are high; doctor warns at 25% or 64 MB.
- Run `muninn doctor` afterwards.
