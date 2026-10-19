---
name: muninn-learn
description: Control bounded muninn learning requests with `muninn learn request|list|show|cancel|retry|replay`. Use when the user asks to review a session, inspect learning progress or receipts, cancel, restart, or explicitly replay a historical window.
---

# muninn learn

    muninn learn request --session REF [--from-line N --through-line N] [--policy PATH]
    muninn learn list [--state STATE]
    muninn learn show R_ID
    muninn learn cancel R_ID
    muninn learn retry R_ID
    muninn learn replay --session REF --from-line N --through-line N --policy PATH

- `REF` identifies an indexed `provider:thread_id` or an unambiguous session root. Request IDs are `R_` plus 32 lowercase hex characters.
- Request bounds must be paired. Without bounds, request uses activation/uncompleted boundary through the latest complete record. A request is admission, not completed review.
- The installed admitted frozen policy is required, even for an explicit policy file, which must match it. Missing/invalid policy refuses `review_policy_required`; never invent runner/model/reasoning, bytes/rounds, deadlines, retries/backoffs, concurrency or budget. This command does not install a policy.
- Request/cancel/retry return current `RequestView`; list returns `requests: [RequestView, ...]`. State is `pending`, `running`, `waiting`, `completed`, `failed` or `cancelled`.
- Show returns `ReviewDetail` with request, attempts, durable calls, governed evidence and receipt/null. Evidence is content, never suitable for logs. Open cited originals before making factual claims.
- Retry is an explicit operator restart of the same request, including cancelled state when current guards permit. Completed requests preserve existing state/receipt without another commit.
- Replay requires explicit bounds and policy and uses the same request/evidence/commit path. Never implicitly enqueue an archive.
- Local correction is `muninn know propose --input FILE`; it uses admitted evidence and checked authority without a model call. Proposal text cannot mint owner authority.
- End/precompact capture and startup context refresh are separate. Busy enqueue is not durable acknowledgment; later recovery cannot prove the exact missed event/cutoff. Cursor waits for the next native import or explicit full ingest.
- Cursor resume/postcompact injection is unproven: pull `muninn know context` explicitly. Already injected context cannot be retracted. Per-prompt recall starts off on fresh installs.

Output is one JSON object, compact unless `--pretty`; hooks always remain compact and exit 0. Writer exit 3 means busy. See `docs/REFERENCE.md` for strict shapes and refusal semantics.
