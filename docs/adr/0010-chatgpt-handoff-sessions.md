# 0010: Classify Codex ChatGPT-handoff sessions as primary

## Status

Accepted

Date: 2026-10-02

## Context

A Codex desktop session that continues a ChatGPT conversation has `thread_source`
`chatgpt_handoff`. Muninn classed every thread source it did not recognise as `other`,
which keeps a source row but stores no events. This excluded handoff sessions and their
subsequent user messages from search, citation and quote checking, preventing decisions
in those sessions from reaching the knowledge ledger.

## Decision

`chatgpt_handoff` is classed `primary`, with the reason kept as
`thread_source=chatgpt_handoff`. The first user message of such a session is one message
holding two sections: the pasted ChatGPT conversation (third-party text that may carry
personal details) and a request that ChatGPT composed from it. A Codex user message with
`## Referenced ChatGPT conversation` in its first 400 characters is stored whole as
`harness`, so it is never a `prompt`, never citable and never pushed as recall.
Subsequent user input follows ordinary `prompt` classification. This change raised
`CLASSIFIER_VERSION` to 3 so existing `other` rows were re-read.

## Rationale

The ledger must distinguish user-authored evidence from generated text. The opening
message includes ChatGPT-authored wording; citing it as a user prompt would misattribute
that wording. Classifying the whole message as `harness` conservatively excludes it from
citation. A future design could split the message at `## My request:` to classify the
two sections separately, if prompt searchability is required.

## Consequences

- Other unrecognised thread sources stay `other`; only this one is promoted, by name.
- The heading check looks 400 characters in, not only at the start, so a short wrapper
  in front of it does not let the pasted conversation through. It applies to any Codex
  user message, so a user-authored message containing the same heading is also
  classified as `harness`.
- The pasted block is still stored (as `harness`), so it can show up in residue scans
  and in `erase`.
- Other injected wrappers (`<send_user_message_question_reply>`,
  `<external_codex_apps_open_page>`, `<in-app-browser-context>`) are not tagged by this
  change and stay `prompt`, as in ordinary Codex sessions.
  `<send_user_message_question_reply>` can carry wording the agent wrote, which a
  citation would then attribute to the user; this attribution risk remains outside this
  classification change.
- The first upgrade after this change re-reads every source once (any classifier version
  bump does).
