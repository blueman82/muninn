# 0022: Trust owner lines by provenance; bind decisions to verbatim text and adjacency

## Status

Accepted by the owner on 2026-11-15.
It changes the rule that ADR 0017:79-80 keeps. It supersedes the reply-sentence parts of
ADR 0018 (:156-176, :192-200); the rest of ADR 0018 stays in force (section 4).
Installation and live verification stay different checks.

## Context

- Owner text gets `direct_owner` only in command shape (verified, muninn/review_authority.py:40-44,
  :106-110), and decisions need it (verified, muninn/review_candidates.py:58, :86-96, :114-142).
- 2026-11-15 model test: Codex cited the right owner lines in 5 of 5 runs, including
  "i agree" after a proposal; Muninn rejected all (reported by team lead; not reproduced).
- `_support` checks authority and type only, not claim against text (verified,
  review_candidates.py:86-96): a trusted "Use pytest" can back "Use Postgres" (inferred).

## Decision

The model judges meaning. Code checks provable facts: who typed it; the words are there.

### 1. Provenance decides owner authority
An `owner_statement` gets `direct_owner` when: primary thread (verified,
muninn/review_evidence.py:40-52); `classify.user_text_kind == "prompt"` (branch f765855);
`injected()` false (verified, review_authority.py:63-80); no tool result in the message
(verified, review_selectors.py:275-276); no harness tag, Claude prefix or ChatGPT tag
anywhere (verified, review_authority.py:92-99); no `FLAG_MARKERS` string (event_model.py:28-33;
new, today only `_QUOTED` stops `<muninn-memory`, verified review_authority.py:16).
Cursor (owner decision 5): bubbles are always `prompt` (verified, f765855 classify.py), so
Cursor owner lines get `direct_owner`; a `stop` hook `followup_message` arrives unlabelled
(cursor.com/docs/agent/hooks; not checked here).

Wording checks, one by one:
| Check | Location (verified) | Guards | Action |
|---|---|---|---|
| `_DIRECT`, `_UNCERTAIN`, comma rule, `_CONTEXT` | review_authority.py:29-44, :109, :134 | wording | remove |
| `_IMPORTED`, `_REPORT`, `_reported` | :17-28, :48-60 | wording guess of a speaker | remove |
| `_QUOTED`, `_IDENTIFIER` mask | :15-16, :87-89 | mostly wording; `<` also stopped tags | remove; tags and markers now checked by name |
| heading/fence rule | :100 | wording ("# AGENTS.md" is a harness tag) | remove |
| harness/ChatGPT tags anywhere; `injected` flags | :92-99; :63-80 | provenance | keep |
| whole-sentence span | :141-155 | integrity (no lost "Never") | keep, as part of binding |

### 2. Verbatim proof plus a corrected copy
- Proof (code-checked): each line of an owner-required claim (review_candidates.py:114-127)
  equals one bound excerpt; lines are note content, each condition, or a guide claim
  (verified, muninn/knowledge_write.py:77-110). Equal: case-sensitive, whitespace collapsed,
  one final `.`/`!`/`;` ignored; the `quote-check` rule (verified, muninn/query/quotes.py:14-41)
  as equality. The excerpt is the redacted `EvidenceRecord.excerpt` (verified,
  review_selectors.py:249-257), whole sentences only.
- Bound excerpts come only from a cited `direct_owner` record or its paired assistant record.
- Corrected copy (owner decision 1, not code-checked): the model also writes a copy with
  spelling and grammar fixed, and for agreement the decision in plain words. No current
  field fits: claim text must stay verbatim, and `reason` means why (verified,
  muninn/knowledge_models.py:99-107). Add an optional `corrected: Text | None` to `NoteBody`
  and `SupportedClaim` (knowledge_models.py:91-96, :99-107).
- Owner views only (decision 6): `know list`, `know show`, `know decision show`/`status`
  print the copy beside the proof (verified, muninn/cli_parser.py:206-240). Proof only: hook
  recall (muninn/hook.py:338), `know context` (cli_know.py:94), review input
  (review_rounds.py:255), `know search` (verified). Recall size is unchanged (inferred).

### 3. Agreement replies, no word list
- A `direct_owner` line binds content through the final `assistant_report` before it
  (verified, review_selectors.py:102-106, :187-197): same thread, no permitted field
  between; validation rereads that gap and fails closed.
- The claim cites both ids; lines equal whole assistant sentences; the model judges agreement.
- No "?" rule (owner decision 3). A question plus the owner reply is the proof; the
  corrected copy states the decision. `_SENTENCE` does not split at `?` (verified,
  review_authority.py:45), so the proof can span the question and the next sentence.
- Scope (decision 4): unchanged. `allow_global` stays `false` (verified,
  tools/default_codex_policy.json:10; muninn/review_evidence.py:266-269): project scope only.

### 4. ADR 0018 approvals: same pattern, no fixed sentence
Today the owner types "Please approve memory D_x." (verified, decision_words.py:16), compared
exactly (verified, decision_proof.py:351, decision_write.py:177); "approved" is refused (:344-345).
- The agent judges whether the reply means approve, reject or edit, and records it in
  `DecisionIntent.action` as today (verified, muninn/decision_models.py:135-153).
- Code checks: the owner reply has `direct_owner` by provenance (section 1) and the intent
  selects the full owner message; the `display_locator` record is the final
  `assistant_report` directly before it, same thread, no permitted field between; the
  display excerpt equals `show_candidate` text of the target (kept, decision_proof.py:353-362,
  decision_write.py:178-180); every `D_` id (pattern at decision_models.py:28) in that full
  assistant message is the intent's `candidate_id` or `replacement_id`.
- One owner reply backs one decision: a second intent with the same owner locator is refused
  (query on stored `locator_json`; no schema change, inferred).
- Remove `decision_words` equality and the "approved" refusal. Keep tombstones, scope,
  saved/erased state, budgets, digest, enrichment (ADR 0018:212-226) and receipts.
- Change `owner_instructions` (decision_words.py:19-33) to "reply in your own words straight
  after this display"; update `.claude`/`.codex` `skills/muninn-know/SKILL.md:94-110`
  (identical, verified diff), docs/REFERENCE.md, docs/memory-lifecycle-design.md.

ADR 0018 reasons for the fixed sentence, and how each is kept:
| Reason (ADR 0018 line) | Kept by |
|---|---|
| Wrong proposal: ID names it (:156, :190) | adjacency + display equals target + no foreign `D_` id |
| Approval by agent, tool, recalled text (:86, :203) | provenance; text copied into the owner's own message now counts (risk) |
| Transfer to edited content (:182) | candidate digest and display equality, unchanged |
| Replay: same reply reused (:313) | `intent_key` UNIQUE (verified, muninn/store_decision_schema.py:44) + one reply, one decision |
| Bare "approved" has no reference (:198) | adjacency gives the reference |
| Negated, conditional, conflicting reply (:194-195) | **NOT kept by code.** The agent judges meaning |

Code cannot prove the last row (ADR 0018:86): code proves the owner replied to that one
display; the agent decides it means approve. Accepted risk (Consequences).

### 5. Versions and digests
- No `PROJECTION_VERSION` bump (verified, voids knowledge, knowledge_read.py:165); no new `Authority`.
- **Result and round schema digests change**: `corrected` is in `ReviewResult`, and
  `FinalRequest` embeds it (verified, review_rounds.py:68-72, :82-84; review_json.py:74-76;
  checked at muninn/review_codex_launch.py:318-324). No table change: bodies live in
  `content_json` (verified, muninn/store_knowledge_schema.py:128); old rows lack the
  optional field (inferred).
- **Prompt digest changes**: copy proofs verbatim, add `corrected`, cite both agreement lines
  (verified, tools/prepare_codex_policy.py:199-220). **Owner re-approval at install** is
  accepted (owner decision 2) and covers every change in this section.
- `owner_instructions` is outside the candidate digest (verified, decision_candidates.py:51-61,
  :336-342); old displays must be shown again (inferred). Rewrite learning-failure-design.md.

## Consequences

- Plain-speech and agreement decisions are learned; approvals need no fixed sentence; each
  memory shows its corrected copy next to the verbatim proof.
- Stored knowledge keeps its authority (verified, knowledge_read.py:239-244).
- Accepted risks (owner, 2026-11-15):
  - The agent can read an unclear reply ("ok wait") as approval, on the ADR 0018 path and
    the agreement path. Code cannot check meaning (section 4).
  - Text the owner pastes into their own message counts as owner text, as in Hermes
    77a30e91 (pastes expand inline, no label). No attachment filter.
  - A Cursor `stop` hook `followup_message` looks like owner text (section 1).
  - The model can misjudge adversarial text. The unchecked corrected copy can differ from
    the proof; only owner views show it, beside the proof. Agents get only the proof.
  - Mitigation: show the verbatim reply or proof; `muninn know retract`.
- Still rejected: hook, harness, meta, sidechain, delegation text; tool results; markers.

## Test impact

- tests/fixtures/learning/cases.json: 0 flip; its 51 `untrusted` entries are 46 tool_result,
  2 observed_outcome, 2 assistant_report, 1 tool_request (verified, script).
- tests/test_review_authority.py: 2 of 4 tests, 61 subtests flip (12 adjuncts x 3 providers
  + 25 of 27 imported texts) (verified by reading :49-108).
- tests/test_review_authority_commit.py:74-127 stays rejected, now by binding (inferred).
- Decision tests (inferred): test_decision_proof.py:42-70 and :314-329 flip; :156-178
  depends on the full-message rule; decision_support.py:103-107 and `display_after=True` change.
- CommitCase owner-kind notes need verbatim content (verified, test_review_commit.py:43, :93).
- Result-schema fixtures change with `corrected`; recall tests must show it absent (inferred).

New tests: provenance per provider; markers; mid-message tag; "Use pytest" vs "Use Postgres";
"Never use pytest." vs "use pytest"; agreement (3617 line 10125 after 10118) and question +
"yes" save; gap or tool call rejects; `corrected` shown with proof; hook "yes" untrusted;
decision "yes" saves; gap, foreign `D_` id, or one reply for two decisions refuses.

## Owner decisions (2026-11-15)

1. Keep both: verbatim proof (code-checked) and a corrected copy (not checked).
2. Re-approval at install: accepted.
3. "yes" to an assistant question: accepted; no "?" rule.
4. Scope: `allow_global` stays `false`; all learning stays project scope for now.
5. Cursor: trust.
6. The corrected copy appears only in owner views; recall prints only the proof.

## Implementation notes (2026-11-15)

These notes record how the source reads the decision above.

- A claim line can equal a run of consecutive whole sentences inside a bound excerpt, not
  only the complete excerpt. Initial evidence selects whole fields, so a one-sentence
  claim from a longer owner line or assistant reply needs this. The excerpt itself must
  still start and end on sentence boundaries.
- The claim line is redacted before the comparison, because excerpts are redacted.
- The paired assistant field must be the last permitted field of its record, and its
  selection must cover whole sentences of the full field.
- When a verified ADR 0018 decision is applied, the decision proof (display equals the
  complete candidate, adjacency, digest) binds the enriched owner reply. Verbatim binding
  does not apply to that owner reply.
- A read error or erased line in an agreement gap refuses the pair; an exhausted read
  allowance refuses the commit with its limit code.
