---
name: muninn-ingest
description: Force muninn to index provider transcripts now with `muninn ingest`. Use when a just-finished session is missing from search, `index_age_s` is large or `poller` is `stale`, or the user asks to re-scan or catch up the index.
---

# muninn ingest

The poller ingests every 60 s. Run manually for immediate indexing.

    muninn ingest            # catch up
    muninn ingest --full     # rescan everything (slower)

Answer fields: `files_seen`, `files_changed`, `events_added`, `events_removed`, `skipped_files`, `unreadable_files`, `skipped_lines`, `failed`, `errors`, `missing`, `duration_s`.

- Acquires the writer separately for each fresh pass. Missing Git membership facts are probed while unlocked, then sources and database state are replanned. There is no total retry/time limit under moving inputs; each probe retains its 10-second timeout. Git facts describe their observation time, not a coherent or current repository snapshot.
- Takes the writer lock: exit code 3 means busy (retry), 4 means the store is unavailable, a hot journal exists, or `tombstone.key` is missing or damaged (`error` `tombstone_key`; restore it from a backup, do not recreate it).
- Counters are invocation work totals: `files_seen`, `skipped_files` and `unreadable_files` count visits/attempts across passes, not distinct files. `files_changed`, event counts and `skipped_lines` sum committed work; `--full` stays full on every pass and counts repeated committed replacements. `failed` and `errors` count ordinary failed read attempts, possibly including subsequently recovered sources; preparation misses are excluded. `missing` comes only from the completed terminal pass. `duration_s` includes preparation and lock waits. A completed sweep can still contain failures.
- Tombstoned (erased) sessions are skipped before any line is read; they count in `skipped_files`. An attempt that cannot open a file (permissions) counts in `unreadable_files` too.
- Provider transcripts are only read, never modified.
- `CODEX_HOME` and `CLAUDE_CONFIG_DIR` select provider history roots; `MUNINN_ROOTS` overrides the entire root map.
- `--full` also imports Cursor from the native absolute `MUNINN_CURSOR_DB` path. On macOS, its established `Library/Application Support/Cursor/User/globalStorage/state.vscdb` path is the default. On Windows and Linux, set `MUNINN_CURSOR_DB` explicitly; no database layout is guessed. The source database is read-only, and erased content stays excluded.
- If `poller` remains `stale`, run `muninn doctor` and check the owned per-user service. Avoid repeated ingest attempts.
