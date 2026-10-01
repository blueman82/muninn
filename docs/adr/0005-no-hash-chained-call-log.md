# 0005: No hash-chained call log for now

Status: Accepted (deferred), 2026-10-01. Decided by: owner ("not item 6").

**Context.** `calls.jsonl` is a bounded, content-free call log with no tamper evidence. A hash chain would make edits or
deleted lines detectable, but it would also need rotation turned off to keep proof for months. The earlier security review
rated it defense-in-depth, not required.

**Decision.** Keep `calls.jsonl` as it is: debugging and support, not evidence.

**Consequences.** The log can be edited undetectably and forgets after about 2 MiB. Revisit if a colleague or auditor needs
proof of what memory an agent was shown; the lines hold only ids and counts, so a chain would stay small.
