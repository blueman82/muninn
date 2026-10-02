---
name: pctx-ingest
description: Force pctx to index provider transcripts now with `pctx ingest`. Use when a just-finished session is missing from search, `index_age_s` is large or `poller` is `stale`, or the user asks to re-scan or catch up the index.
---

# pctx ingest

The poller normally does this every 60 s. Run it by hand when you need new content immediately.

    pctx ingest            # catch up
    pctx ingest --full     # rescan everything (slower)

Answer fields: `files_seen`, `files_changed`, `events_added`, `skipped_files`, `skipped_lines`, `failed`, `errors`, `missing`, `duration_s`.

- Takes the writer lock: exit code 3 means busy (retry), 4 means the store is unavailable or a hot journal exists.
- Tombstoned (erased) sessions are skipped before any line is read, by design; they count in `skipped_files`.
- Provider transcripts are only read, never modified.
- If `poller` stays `stale`, run `pctx doctor` and check the launchd job instead of looping ingest.
