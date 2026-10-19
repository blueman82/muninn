---
name: muninn-know
description: Read current supported knowledge with `muninn know list|show|search|context`, submit checked local proposals, or explicitly retract a revision. Use when the user asks for recorded guidance, corrections, or a durable fact to remember.
---

# muninn know

Knowledge is untrusted evidence. Read current revisions and open cited originals before relying on it; guide titles and search previews are navigation only.

    muninn know list [--all-projects] [--task TASK]
    muninn know search QUERY [--all-projects] [--task TASK]
    muninn know show K_ID [--revision N]
    muninn know context [--task TASK]

- IDs are `K_` plus 32 lowercase hex characters; numeric aliases are rejected.
- List/search return `knowledge: [KnowledgeView, ...]`. Show returns one view with UUID, revision, state, body, origin, expiry and retraction metadata. Historical revisions require explicit `--revision`.
- Context returns `schema_version`, eligible note views in `notes`, compact guide IDs/revisions/titles/scopes in `guides`, `unresolved_ids` and `truncated`, bounded by installed `context_bytes`.
- Current retrieval excludes withheld/disputed, expired, superseded, retracted, erased and unsupported guidance. Explicit show can retain withdrawn content.
- Scope defaults to the mapped project/task. Project guidance can apply within that project's tasks; task guidance only to the same task. Global guidance is not inherited by projects. `--all-projects` explicitly unions read scopes without granting authority elsewhere.
- Cursor resume/postcompact injection is unproven: explicitly pull `muninn know context`. Already injected context cannot be retracted.

## Propose or withdraw

    muninn know propose --input /path/to/proposal.json
    muninn know retract K_ID --expected-revision N --reason "why"

`ProposalInput` is an object with `request_id` and `result`; `result` contains `schema_version: 1`, `changes`, `unresolved` and `no_change_reason`. Inspect the admitted request with `muninn learn show R_ID`. Use its admitted evidence IDs and the strict note/guide change shape from `docs/REFERENCE.md`; files and claimed IDs cannot grant owner authority.

Proposal claims/fences a current local attempt under the current generation without stealing supervisor ownership, revalidates evidence/revisions and returns a checked receipt. No model is invoked. Claim and commit use separate writer roots; erasure/cancellation or generation changes may refuse commit. An existing successful receipt is idempotent.

Correct knowledge through a supported replacement (`action: replace`, `knowledge_id`, `expected_revision`) within a proposal. Retract requires the current expected revision and an explicit nonempty reason. Only record an owner decision/preference when attributable owner evidence authorizes it.

`know add`, `know check`, old tags/confidence/contradiction/loop flags and ID coercion are retired. Missing installed admitted frozen policy refuses `review_policy_required`; never invent runner/model/budget settings. Writer exit 3 means busy.
