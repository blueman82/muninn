# 0010: Codex ChatGPT-handoff sessions are the owner's primary sessions

Status: Accepted, 2026-10-02. Decided by: owner (answered yes when asked whether to index them).

**Context.** A Codex desktop session that continues a ChatGPT conversation has `thread_source` `chatgpt_handoff`. Muninn
classed every thread source it did not recognise as `other`, which keeps a source row but stores no events. Four of the
owner's sessions fell in that class, one of them with 18 messages the owner typed. Nothing in them could be searched, cited or
quote-checked, so a decision made there could not reach the knowledge ledger.

**Decision.** `chatgpt_handoff` is classed `primary`, with the reason kept as `thread_source=chatgpt_handoff`. The first
user message of such a session holds the pasted ChatGPT conversation, which is third-party text and may carry personal
details. Text that starts with `## Referenced ChatGPT conversation` is stored as `harness`, so it is never a `prompt`, never
citable and never pushed as recall. What the owner types in the session is stored as `prompt` like any other.
`CLASSIFIER_VERSION` goes to 3 so the existing `other` rows are read again.

**Reasoning.** The owner's own typing is the evidence the ledger exists to cite, and these sessions are the owner's. The
pasted block is the only part that is not.

**Consequences.**
- Other unrecognised thread sources stay `other`; only this one is promoted, by name.
- The injected context blocks that sit in front of the owner's request in the same message (browser state, mentioned files)
  are not split off: the message stays a `prompt`, as it already is in ordinary Codex sessions.
- The pasted block is still stored (as `harness`), so it can show up in residue scans and in `erase`.
- The first upgrade after this change re-reads every source once (any classifier version bump does).
