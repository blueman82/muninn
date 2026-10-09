# 0013: Import Cursor history during full ingest

## Status

Accepted

## Context

Muninn indexes Claude Code and Codex transcripts automatically. Cursor's local SQLite
format is undocumented and may change, and some users do not have Cursor installed on
the machine where Muninn runs.

## Decision

On a fresh install, detect Cursor's standard macOS database location and import it
during the existing full ingest when it is present. The importer opens SQLite read-only,
imports supported visible user and assistant text from `cursorDiskKV`, and skips
unsupported and malformed rows. Do not poll Cursor databases. Store the events as
provider `cursor`, and widen the store constraints through the one-way schema v3
migration.

An optional Cursor `preCompact` hook refreshes only the unique exact
`composerData:<conversation_id>` from Cursor's documented hook payload, with
`workspace_roots` binding the requested scope and `message_count` as the bubble
processing limit plus one oversize-detection probe. Missing, ambiguous or mismatched identity or workspace skips before
writes; the hook does not fall back to the newest database row. The hook-to-store
identity mapping is empirical, with sources and compatibility limits in the
[quick start](../QUICKSTART.md#cursor-history). If the stored conversation
has more bubbles than Cursor reported, skip it whole rather than replacing
stored events with a partial import. A full ingest remains the complete
history import path. This is event-triggered import, not background polling.

## Consequences

- Cursor text is searchable and erasable through the same event and tombstone paths as
  other providers.
- A full `ingest` pass can refresh Cursor history; regular polling and `rebuild` do not
  read Cursor's database.
- Cursor's undocumented format can drift, so imports remain conservative and may skip
  rows that a future version does not recognize.
