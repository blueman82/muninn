---
name: muninn-stats
description: Show muninn counts and index health with `muninn stats`. Use when the user asks how big the memory is, how many sessions or events are indexed, when the poller last ran, what is being skipped, or which release is installed.
---

# muninn stats

    muninn stats [--usage] --pretty

Useful fields:
- `events`, `events_by_provider`, `sources` (provider/root/thread class/status).
- `last_pass`: `files_changed`, `events_added`, `skipped_files`, `failed`, `errors`, `index_age_s`, `poller`.
- `db_bytes`, `db_space.free_ratio` (high ratio: consider `muninn compact`).
- `install_sha`: the pinned release; compare with `git rev-parse HEAD` to see if an upgrade is pending.
- `hash_mismatches`, `skipped_lines`, `issues`, `flags` (marker, redacted, truncated).
- `--usage` adds per-session counts of muninn calls seen in transcripts.

`skipped_files` counts tombstoned, unidentifiable or duplicate files. Erased sessions whose provider files still exist keep counting until those files are removed. Counts only, never transcript text.
