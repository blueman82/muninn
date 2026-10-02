---
name: muninn-compact
description: Reclaim muninn database space with `muninn compact`. Use when `muninn doctor` warns db_free_space or `muninn stats` shows a high `db_space.free_ratio`, or when the user asks to shrink or vacuum the muninn database.
---

# muninn compact

    muninn compact --pretty

Runs VACUUM under the writer lock and reports `compact.bytes_before` / `bytes_after`.

- It needs roughly one database of free disk. It refuses otherwise; do not work around the refusal.
- It blocks the poller and other writers while it runs. Exit code 3 means a writer holds the lock: wait and retry.
- Compaction is only worthwhile when free pages are high (doctor warns at 25% or 64 MB). Check `muninn stats` first.
- Run `muninn doctor` afterwards.
