---
name: muninn-stats
description: Show muninn counts and index health with `muninn stats`. Use when the user asks how big the memory is, how many sessions or events are indexed, when the poller last ran, what is being skipped, or which release is installed.
---

# muninn stats

    muninn stats [--usage] --pretty

## Output fields

- `events`, `events_by_provider`, `sources` (provider/root/thread class/status).
- `last_pass`: `files_changed`, `events_added`, `skipped_files`, `unreadable_files`, `last_error` (error class name, null after the poller's next successful pass), `failed`, `errors`, `index_age_s`, `poller`, and `alive_age_s` (seconds since a running pass last stamped alive; null between passes, and growing if the poller was killed mid-pass and not restarted).
- `reread`: `pending` (active sources still to be re-read after an upgrade changed the classifier) and `of` (all active sources). `pending` normally falls to 0; sources that fail or are skipped stay pending, so check `last_pass.failed`, `unreadable_files` and `skipped_files`.
- `knowledge` by durable state (`active`, `withheld`, `retracted`, `erased`); state alone is not current eligibility. `evidence` by state (`live`, `changed`, `erased`).
- `review`: schema 1, durable `requests_by_state`, `calls_by_state`, nonnegative/null `oldest_pending_age_seconds`, `last_progress_age_seconds`, `unresolved_items` and nullable actual `usage`. Missing usage and unconcluded/unknown calls remain null even with retained usage; reservations are not measured charges; mixed/unknown or unsafe currencies leave cost/currency null. Diagnostic currency must match `[A-Z]{3}`.
- `review.eligible_records` and `completed_records` count currently indexed logical prompt/reply lines once per provider/thread/line. Exclude activation archive unless explicitly admitted for replay; completion needs a success receipt (including no-change) plus current indexed provenance: matching admitted endpoint and matching live same-request evidence for each interior record. Endpoint equality alone proves no interior record. Missing/changed/erased inputs remain uncertain gaps, conservatively reducing metric availability while preserving audit receipts. Health reads no originals. Overlap and multipart records count once. Later receipts can coexist with earlier gaps; replay preserves the activation baseline. These are not absolute cursors or lifetime erased-content counts.
- Durable coverage survives disabled/rotated logs. `observed_enqueue_busy` counts only retained busy log observations (null if unavailable); `busy_count_complete` is always false. Zero observations do not prove no lost acknowledgment. Recovery captures later content, not exact lost events/cutoffs; Cursor waits for native import or explicit full ingest. Exact per-source refusal reasons are not durable.
- `db_bytes`, `db_space.free_ratio` (high ratio: consider `muninn compact`).
- `install_sha`: the pinned release; compare with `git rev-parse HEAD` to identify a pending upgrade.
- `hash_mismatches`, `skipped_lines`, `issues`, `flags` (marker, redacted, truncated).
- `retrieval_outcomes`: from the call logs, `window_lines` (parseable log lines counted) plus `search` (`calls`, `zero`, `outside`, `noterms`) and `hook_prompt` (`calls`, `zero`, `outside`, `floor`) counts of calls that found nothing and why. These are facts about calls, not proof that a memory was missed.
- `--usage` adds per-session counts of muninn calls seen in transcripts.

`skipped_files` counts tombstoned, unidentifiable or duplicate files. Erased sessions whose provider files still exist keep counting until those files are removed. Counts only, never transcript text.
