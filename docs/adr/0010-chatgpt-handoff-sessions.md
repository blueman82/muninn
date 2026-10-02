# 0010: Codex ChatGPT-handoff sessions are the owner's primary sessions

Status: Accepted, 2026-10-02. Decided by: owner (answered yes when asked whether to index them). The treatment of the opening
message below is the implementer's, inside that work, and is the one point to revisit.

**Context.** A Codex desktop session that continues a ChatGPT conversation has `thread_source` `chatgpt_handoff`. Muninn
classed every thread source it did not recognise as `other`, which keeps a source row but stores no events. Four of the
owner's sessions fell in that class, with messages the owner typed. Nothing in them could be searched, cited or quote-checked,
so a decision made there could not reach the knowledge ledger.

**Decision.** `chatgpt_handoff` is classed `primary`, with the reason kept as `thread_source=chatgpt_handoff`. The first
user message of such a session is one message holding two sections: the pasted ChatGPT conversation (third-party text that
may carry personal details) and a request that ChatGPT composed from it. A Codex user message with
`## Referenced ChatGPT conversation` in its first 400 characters is stored whole as `harness`, so it is never a `prompt`,
never citable and never pushed as recall. What the owner types afterwards is stored as `prompt` like any other.
`CLASSIFIER_VERSION` goes to 3 so the existing `other` rows are read again.

**Reasoning.** The owner's own typing is the evidence the ledger exists to cite, and these sessions are the owner's. The
opening message is not their typing: the request section is written in the third person about the owner, so citing it
would attribute ChatGPT's wording to them. Treating it as `harness` errs towards not citing. If the owner wants the request
section searchable as a prompt, the message can be split at its `## My request:` heading into two events.

**Consequences.**
- Other unrecognised thread sources stay `other`; only this one is promoted, by name.
- The heading check looks 400 characters in, not only at the start, so a short wrapper in front of it does not let the pasted
  conversation through. It applies to any Codex user message, which is harmless: a person does not type that heading.
- The pasted block is still stored (as `harness`), so it can show up in residue scans and in `erase`.
- Other injected wrappers (`<send_user_message_question_reply>`, `<external_codex_apps_open_page>`,
  `<in-app-browser-context>`) are not tagged by this change and stay `prompt`, as in ordinary Codex sessions.
  `<send_user_message_question_reply>` can carry wording the agent wrote, which a citation would then attribute to the owner;
  that is a separate, older problem.
- The first upgrade after this change re-reads every source once (any classifier version bump does).
