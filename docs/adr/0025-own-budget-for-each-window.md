# 0025: Each window gets its own read budget

## Status

Proposed, 2026-11-19. This decision changes ADR 0023 for the identity
read and the boundary read of live capture and catch-up.

## Context

A hook capture used one read budget (policy `max_input_bytes`, 1 MiB) for
the line-1 identity read and for all new windows of one chat. A long
session with more than approximately 1 MiB of new text used all of that
budget. The capture then stopped with `evidence_limit` and admitted no
window. Catch-up also used one budget for the identity read, the boundary
read and the window. A window that did not fit stopped again at each
catch-up pass, so learning stopped for that chat.

ADR 0023 removed the charge for lines before the window. It kept the
charge for the identity read of line 1 and for the read of the last
eligible boundary line.

## Decision

1. In a hook capture and in catch-up, each window gets its own new
   `ReadBudget(max_input_bytes)` for its admission check. The reads of one
   window do not charge the budget of a different window. The catch-up
   index queries keep their own budget for each pass.
2. The line-1 identity read (`hook_learn.validated_source` and
   `review_catchup._native`) and the read of the last eligible boundary
   line (`review_progress.reconciliation_windows`) charge no bytes and no
   records. The 8 MiB record cap and each hash check stay.
3. Before admission, Muninn cuts a JSONL window from its start until it
   holds at most 200 lines and at most `review_split.safe_limit` original
   bytes. Muninn gets the bytes from the index offsets. When the last line
   alone is larger than the safe limit, or when the admission check still
   meets a read limit, Muninn skips the window.
4. A cut or a skip writes one `review_skipped` record to `calls.jsonl`
   with `error_code` and `skipped_lines`. The record has no transcript
   text and no capture gap. Skipped lines stay examined. They do not move
   `completed_line`.

The Cursor reader and Cursor windows do not change.
`review_requests.reconcile_sources` keeps its shared budget.

## Consequences

A long chat is learned in full. A very large part of a chat is skipped one
time with a log line, and later parts are learned as usual.

Each window reads the prefix of the file again, as ADR 0023 describes. A
capture of many windows thus reads more bytes in total than before. Each
window stays inside its own limit.

The size of a line comes from the index offsets. Lines with no index
event after a line count as part of that line. Thus Muninn can skip a
window when a large line with no index event follows its last line.

Requests that failed with a read-limit code before this decision stay
failed. Muninn does not replay them.
