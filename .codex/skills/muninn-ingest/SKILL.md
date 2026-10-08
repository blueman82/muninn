---
name: muninn-ingest
description: Force muninn to index provider transcripts now with `muninn ingest`. Use when a just-finished session is missing from search, `index_age_s` is large or `poller` is `stale`, or the user asks to re-scan or catch up the index.
---

# muninn ingest

The poller normally does this every 60 s. Run it by hand when you need new content immediately.

    muninn ingest            # catch up
    muninn ingest --full     # rescan everything (slower)

Answer fields: `files_seen`, `files_changed`, `events_added`, `events_removed`, `skipped_files`, `unreadable_files`, `skipped_lines`, `failed`, `errors`, `missing`, `duration_s`.

- Takes the writer lock: exit code 3 means busy (retry), 4 means the store is unavailable, a hot journal exists, or `tombstone.key` is missing or damaged (`error` `tombstone_key`; restore it from a backup, do not recreate it).
- Tombstoned (erased) sessions are skipped before any line is read, by design; they count in `skipped_files`. A file that cannot be opened (permissions) counts in `unreadable_files` too.
- Provider transcripts are only read, never modified.
- `CODEX_HOME` and `CLAUDE_CONFIG_DIR` select provider history roots; `MUNINN_ROOTS` overrides the entire root map.
- `--full` also imports Cursor from the native absolute `MUNINN_CURSOR_DB` path. On macOS, its established `Library/Application Support/Cursor/User/globalStorage/state.vscdb` path is the default. On Windows and Linux, set `MUNINN_CURSOR_DB` explicitly; no database layout is guessed. The source database is read-only, and erased content stays excluded.
- If `poller` stays `stale`, run `muninn doctor` and check the launchd job instead of looping ingest.
