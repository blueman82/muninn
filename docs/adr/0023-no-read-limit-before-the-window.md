# 0023: No read limit before the window

## Status

Accepted by the owner, 2026-11-16.
Changed by [ADR 0025](0025-own-budget-for-each-window.md): the identity read and the boundary read of live capture and catch-up charge no bytes and no records.

## Context

Muninn reads a JSONL session file from line 1 before each learning task.
Index offsets cannot prove current line numbers after a provider rewrites the start of a file.
Each line before the window counted against three limits:
the task read budget (policy `max_input_bytes`, 1 MiB), 4,096 records, and 32 MiB of scanned bytes.

Measurements on the owner's live store (read-only):

- The call log has 2,859 admission refusals with `evidence_limit`.
- No learning task covers a line after approximately line 413.
- 1,479 of 3,794 sessions are larger than 1 MiB.
- 54 sessions have more than 4,096 lines. 12 sessions are larger than 32 MiB.
- A full read of an 80 MiB, 34,086-line file with a SHA-256 for each line took 0.16 s.

## Decision

Lines before the window start do not count against the byte budget, the record limit or the scan byte limit.
Muninn still reads them from line 1 to prove line numbers.
Each line before the window must still be a complete record of at most 8 MiB.

Lines inside the window count against the byte and record allowances as before.
The model input byte limit and the other budgets do not change.

The window start is the first line of the evidence window.
A boundary check that reads only the last line still charges each line from the window start.
History confirmation hashes each line from line 1 as it reads it, but it charges each line from the window start.

The Cursor reader is unchanged.
Its walk from the first bubble is also limited by database work, and it reads all keys of the conversation.

Before this decision, the read budget (policy `max_input_bytes`), the record limit and the scan byte limit counted each line from line 1.

## Consequences

Each task reads the prefix again.
Measured: one full read of an 80 MiB, 34,086-line file, with a SHA-256 for each line, took 0.16 s.

Estimates (not measured):

- One task reads the prefix one or two times (boundary check, then evidence read).
  The worst case for each task is approximately 0.16 s x 1.5 = 0.25 s.
- Over the life of the largest session, approximately 960 tasks x 0.25 s = 240 s, or 4 minutes.
  The task count is an assumption, not a measurement.
- A history scan checks windows of at most 200 lines.
  Each window reads from line 1 two times: the boundary check and the content hash.
  The largest session has at least 34,086 / 200 = 171 windows.
  A read stops at its window, so it reads half of the file on average.
  The scan cost is approximately 171 x 2 x 0.08 s = 27 s for that session.
  The history scan holds the writer lock for this time.

Long sessions can be learned at any line.
A window must still fit its own allowances.
A provider file with a line before the window that is larger than 8 MiB or incomplete stays unavailable.
