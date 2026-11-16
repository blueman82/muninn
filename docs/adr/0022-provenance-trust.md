# 0022: Trust owner lines by provenance; Muninn copies the full cited message as proof

## Status

Accepted by the owner on 2026-11-15. Revised and accepted 2026-11-16 (full-message proof,
2,000-byte agent-path cap). It changes the rule ADR 0017:79-80 keeps and supersedes
ADR 0018 :156-176, :192-200; the rest of ADR 0018 stays (section 4).
Installation and live verification stay different checks.

## Context

- Owner text gets `direct_owner` only in command shape (verified, muninn/review_authority.py:40-44,
  :106-110), and decisions need it (verified, muninn/review_candidates.py:58, :86-96, :114-142).
- 2026-11-15 model test: Codex cited the right owner lines in 5 of 5 runs, including
  "i agree" after a proposal; Muninn rejected all (reported by team lead; not reproduced).
- (changed 16 Nov) The 2026-11-15 version bound claims to verbatim sentences. Its
  implementation (branch feat/adr0022-provenance-trust-20261115, muninn/review_binding.py;
  notes at that branch's docs/adr/0022-provenance-trust.md:160-175) showed this is brittle
  (reported by team lead): a list without full stops is one sentence, so one item cannot be
  kept; a run of sentences can drop a condition ("If you are on Windows only. Use cmd.exe."
  binds "Use cmd.exe."); per-claim gap rereads can exhaust the read budget.

## Decision

The model judges meaning and writes the decision text. Code checks only provable facts:
who typed the message, and which messages are the proof. Muninn copies the proof itself.

### 1. Provenance decides owner authority
An `owner_statement` gets `direct_owner` when: primary thread (verified,
muninn/review_evidence.py:40-52); `classify.user_text_kind == "prompt"` (branch f765855);
`injected()` false (verified, review_authority.py:63-80); no tool result in the message
(verified, review_selectors.py:275-276); no harness tag, Claude prefix or ChatGPT tag
anywhere (verified, review_authority.py:92-99); no `FLAG_MARKERS` string (event_model.py:28-33).
Remove the wording checks: `_DIRECT`, `_UNCERTAIN`, comma rule, `_CONTEXT`, `_IMPORTED`,
`_reported`, `_QUOTED`, fence rule (verified, review_authority.py:15-60, :87-110, :134).
Cursor (owner decision 5): bubbles are always `prompt`, so Cursor owner lines are trusted.

### 2. Proof is the full cited field, copied by Muninn (changed 16 Nov)
- The model cites evidence by id only. It copies no text as proof.
- Owner support needs a cited `direct_owner` record whose selector covers the full field
  (`start` and `end` null; verified, muninn/review_evidence_models.py:64-83). A partial
  span gives no owner support. Initial evidence selects full fields (reported, branch ADR).
- The proof is that full redacted field (verified, review_selectors.py:249-257). No sentence
  splitting, no verbatim compare, no redact-before-compare step. The branch's `verbatim`,
  `claim_lines`, `bound` and `_final_sentences` (review_binding.py) are dropped.
- The model's claim text (note `content` and conditions, guide claim text) is the decision
  text, unchecked. These fields exist today (verified, muninn/knowledge_models.py:91-107),
  so the branch's separate `corrected` field (knowledge_models.py:100-123) goes away.

### 3. Agreement replies (changed 16 Nov)
- For agreement, the claim cites the owner reply and the final `assistant_report` directly
  before it (verified, review_selectors.py:102-106, :187-197): same thread, no permitted
  field between. The proof is both full fields.
- Muninn checks adjacency once per (assistant, owner) pair per batch, not per claim. A read
  error, erasure or exhausted budget refuses the pair.
- No "?" rule (owner decision 3). The model judges agreement and writes the decision.
- Scope (decision 4): `allow_global` stays `false` (verified,
  tools/default_codex_policy.json:10; muninn/review_evidence.py:266-269).

### 4. ADR 0018 approvals
The agent records approve, reject or edit in `DecisionIntent.action` (verified,
muninn/decision_models.py:135-153). Code checks: owner reply provenance, full field; the
display record is the final `assistant_report` directly before it, no permitted field
between; the display equals `show_candidate` text (verified, decision_proof.py:353-362,
decision_write.py:178-180); every `D_` id in that message is the intent's `candidate_id`
or `replacement_id`; one owner reply backs one decision. Remove `decision_words` equality
(decision_proof.py:351, decision_write.py:177) and the "approved" refusal (:344-345).
Update `owner_instructions` (decision_words.py:19-33) and both `skills/muninn-know/SKILL.md`.

| ADR 0018 reason for the fixed sentence | Kept by |
|---|---|
| Wrong proposal (:156, :190) | adjacency + display equals target + no foreign `D_` id |
| Agent, tool or recalled text (:86, :203) | provenance; owner-pasted text counts (risk) |
| Transfer to edited content (:182) | candidate digest and display equality |
| Replay (:313) | `intent_key` UNIQUE (verified, store_decision_schema.py:44) + one reply, one decision |
| Negated or conditional reply (:194-195) | **not by code**: the agent judges meaning (risk) |

### 5. Who sees what, and size (changed 16 Nov)
- Owner views show the model text beside the proof: `know list`, `know show`,
  `know decision show`, `know decision status` (verified, muninn/cli_parser.py:206-240).
- Agent and model paths show only the proof: hook recall (verified, muninn/hook.py:338),
  `know context` (verified, muninn/cli_know.py:94), `know search`, review model input
  (verified, review_rounds.py:255).
- Size: a proof is one or two full messages, each up to 64 KiB (verified, `TEXT_LIMIT`,
  muninn/event_model.py:37). Context is capped at 12,000 bytes (verified,
  muninn/review_models.py:185; tools/default_codex_policy.json:11). `knowledge_context`
  stops at its limit and sets `truncated` (verified, muninn/hook.py:338-350). One long
  assistant proposal can fill recall and push other memories out (inferred).
- Proposed cap (owner question): in agent paths, a proof over 2,000 bytes is not cut; the
  agent gets the memory id and "proof too long: run `muninn know show K_x`". No partial
  proof, so no lost condition (inferred). Owner views always show the full proof.

### 6. Versions and digests (changed 16 Nov)
- Result schema digest: no change versus main; no new field (verified, main
  muninn/knowledge_models.py:91-107; muninn/review_json.py:74-76).
- Round schema digest: no change versus main (verified, review_rounds.py:68-72, :82-84).
- Prompt digest: changes versus main. One added sentence: "For agreement, cite the owner
  reply and the final assistant reply directly before it." The branch's verbatim and
  `corrected` sentences are dropped (verified, branch tools/prepare_codex_policy.py diff).
  Owner re-approval at install: accepted (owner decision 2).
- Store, `PROJECTION_VERSION`, `Authority` values, `allow_global`: no change.

## Implementation requirements (changed 16 Nov)

1. Add `<user_action>` and `<send_user_message_question_reply>` to Codex `HARNESS_TAGS`
   (absent today; verified, grep of muninn/codex_events.py). Refuse owner text that starts
   with `<name>` and ends with `</name>`.
2. Check the owner line and the display or agreement line against the indexed `event`
   table: same `source_id`, `line`, `line_sha256`, and `kind` `prompt` or `reply` (columns
   verified, muninn/store_schema.py:104-114). Fork and replay copies cannot back a decision.
3. Tests: agent-facing CLI and hook output never contain the model text; a `<muninn-recall`
   marker test; Claude Code shape adjacency tests (scratch test:
   /private/tmp/claude-501/-Users-harrison-Github-muninn/3bd7a8e6-2667-4302-9786-2e2d3177597f/scratchpad/claude_adj_test.py).
4. Document: in-flight learning requests and pre-upgrade staged candidates fail once after
   the upgrade, then work normally. Pre-upgrade decision displays must be shown again.

## Consequences

- Plain-speech and agreement decisions are learned. Lists and conditions stay whole,
  because the proof is the full message. Approvals need no fixed sentence.
- Agents get the owner's and the assistant's real words, not the model's text.
- Stored knowledge keeps its authority (verified, knowledge_read.py:239-244).
- Accepted risks (owner, 2026-11-15):
  - The agent can read an unclear reply ("ok wait") as approval (both paths).
  - Text the owner pastes into their own message counts as owner text (as Hermes 77a30e91).
  - A Cursor `stop` hook `followup_message` looks like owner text.
  - The model can misjudge adversarial text. Its text can differ from the proof; only
    owner views show it, beside the proof.
  - Mitigation: show the proof; `muninn know retract`.
- (changed 16 Nov) Long proofs use recall bytes (section 5).

## Test impact

- tests/fixtures/learning/cases.json: 0 flip (verified, script; its 51 `untrusted` are non-owner).
- tests/test_review_authority.py: 2 of 4 tests, 61 subtests flip (verified by reading :49-108).
- tests/test_review_authority_commit.py:74-127: full-field cases (passage `None`) flip to
  saved; partial-span cases stay rejected (inferred).
- Decision tests (inferred): test_decision_proof.py:42-70 and :314-329 flip; :156-178
  depends on the full-field rule; decision_support.py:103-107 changes.
- (changed 16 Nov) Branch tests for verbatim binding and `corrected` are removed (inferred).
- New: provenance per provider; markers; new harness tags and `<name>` wrappers; event-table
  check rejects fork copies; partial span gives no owner support; agreement (3617 line 10125
  after 10118) and question + "yes" save; gap or tool call rejects; one adjacency read per
  pair; long proof gives the pointer line; decision "yes" saves; foreign `D_` id refuses.

## Owner decisions

1. (2026-11-16) The proof is the full cited message, copied by Muninn; model text unchecked.
2. Re-approval at install: accepted.
3. "yes" to an assistant question: accepted; no "?" rule.
4. `allow_global` stays `false`; all learning stays project scope for now.
5. Cursor: trust.
6. The model text appears only in owner views; agent paths print only the proof.

## Open question for the owner

1. Agent-path cap: a proof over 2,000 bytes becomes a "run `muninn know show`" line.
   Accept 2,000 bytes, another number, or no cap?

Owner answer (2026-11-16): accept the 2,000-byte cap for agent paths (hook recall, know context, know search, model input). Over the cap the agent gets the memory id and "proof too long: run muninn know show K_x". Owner views show the full proof.

## Implementation notes (2026-11-16)

- Authority: a partial-span selection gets `untrusted`, so it gives no owner support.
  A whole message wrapped in one tag (`<name>...</name>`, attributes allowed) is refused
  on every provider. An owner line needs an indexed `prompt` event (`build_record`).
- Support (`review_binding.owner_supported`): a claim needs a cited full `direct_owner`
  message; each cited assistant reply must be a full field paired with a cited owner
  message, or the claim is refused. When a verified ADR 0018 decision is applied, the
  approval reply that enrichment adds proves each claim it backs; the owner approved the
  complete display, so other cited replies need no pairing.
- Adjacency (`review_binding.adjacent_gap`) also needs an indexed `reply` event for the
  assistant line; the commit caches one result per (assistant, owner) pair.
- Agent paths (`knowledge_proof.proof_view`: recall, `know context`, `know search`,
  `muninn search` previews, review reads) replace the claim text with the proof only for
  owner-supported knowledge: preferences, decisions and global knowledge (team lead Q1,
  option A). Facts and procedures keep the model text, as before. The proof marks each
  message `[owner]` or `[assistant]`. A missing proof fails closed:
  "proof unavailable: run muninn know show K_x".
- Knowledge saved through an ADR 0018 approval gives agents the model text the owner
  approved (owner decision Q2, 2026-11-16), found through the knowledge version's
  application request.
- Owner views: `know list` adds `proofs` (by knowledge ID), `know show` adds `proof`,
  and the decision display text and `know decision status` add `proof`.
- A Codex message with scalar `content` has no indexed event, so it cannot be a display.
- Upgrade: in-flight learning requests and candidates staged before the upgrade fail
  once (stored evidence authority changes), then work normally. Pre-upgrade decision
  displays must be shown again (the display text changed).

- Session start (owner request, 2026-11-16): the memories are one numbered list between
  two `▓` bars (20 to 72 characters wide), text wrapped at 70, a `When:` line for
  conditions, and one `▓ n: K_x` code line for each memory; no kind, revision, scope,
  evidence IDs or expiry. The frame, usage, search and open lines stay.
- Pages: every eligible memory in reader order is split into consecutive pages that fit
  the provider's start-block limit (Claude and Cursor 4,000 bytes, Codex 2,800). Page 1
  is the start block; `muninn know context --page N [--provider P]` prints page N as
  text. A memory larger than a page is shown alone as "memory too long: run muninn know
  show K_x". The footer "▓ k more memories: muninn know context --page N+1" replaces the
  old omission line; Codex adds `--provider codex`. Page 1 now stops at the first memory
  that does not fit, where the old block skipped it and tried later ones.
