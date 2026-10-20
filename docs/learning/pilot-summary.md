# Native Codex pilot observation summary

This bounded pilot completed six successor trials on one Mac using Codex CLI 0.163.0, requested gpt-6.1-sol/low. Sources were synthetic. The six failed/stopped predecessor rows remain retained separately.

## Observations

- Twelve new CLI launches; fourteen total including the canary and failed first learner. All twelve successor claims completed and were cleaned.
- $70 reserved; actual provider cost and internal model request count are unknown. Reservations are not a provider-side spending ceiling.
- Reported successor tokens: 271,514 input and 1,572 output. Call latency: 5.01 seconds minimum, 6.14 median, 10.99 maximum; these exclude complete foreground/service latency.
- Literal and procedural paraphrase answers with knowledge included matched the declared task requirements on manual inspection. Withheld answers correctly said information was insufficient. The omitted-context case made no new save and did not invent missing instructions. These observations are not formal independent grades.
- The paraphrase-present run surfaced and opened a procedure guide. It also returned lexical search hits; this live run does not demonstrate recovery from an FTS miss.
- Present and withheld arms learned different content/forms despite matched source/schedule. Their learned-state digests differ. The task results therefore do not provide a clean paired causal estimate of memory benefit.

## Supplemental same-state task comparison

Six additional Codex task calls reused each present-arm saved exposure for both prompt variants. The two arms of every pair had the same learned-state and exposure digests; only the knowledge payload differed. All six completed. The literal and paraphrase answers supplied the recorded procedure when knowledge was present and reported insufficient information when it was withheld. In the omitted case, both arms reported insufficient instructions; the present arm exposed a title but no procedure body. These are manual observations on reused pilot cases, not independent grades or heldout results.

The supplement used six new CLI launches and reserved $30. Across the canary, failed first learner, original successor and supplement, the account records twenty launches and $100 reserved. Actual provider cost and internal model request count remain unknown. Sealed replay made no additional launch. The supplemental result is privately bound by SHA-256 `dbd7722db5ae9fac872283f8b0a975a842baacd38f1f50c2256b13822a1e2c91`.

## Pinned current-Muninn baseline

The pinned `5c1a69b` runtime ingested the same three synthetic chronological cases in a disposable home with recall off and no manual save action. Transcript search returned 1, 2 and 2 hits as history accumulated; its knowledge ledger and knowledge search stayed empty. This is a measured capture and retrieval baseline, not a later-task answer or a test of manually assisted learning. It made no model calls. Its private result is bound by SHA-256 `3ffeb7d5a64962bcead9ed4547c6aae9daa5e09988fed8f42d60bf75f410648f`.

## Acceptance boundary

One family, three logical cases, no independent judgments, no Hermes measurement, and no untouched heldout assessment. The baseline has no matched task-answer measurement. Report status remains `unverified`. A disposable Codex 0.163.0 plugin probe resolved all four hooks and completed ten direct synthetic invocations with admission disabled (private report SHA-256 `98833dcfca1ea1a100ee62fe9d67888324306ca8895546a476032f77fa7245f4`). These checks do not prove interactive hook delivery, launchd service behavior, preventive no-tools/no-MCP isolation, hard provider-side cost limits or production admission. Full Task15 and the final Assessment freeze remain unfinished.
