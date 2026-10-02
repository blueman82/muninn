---
name: pctx-know
description: Manage the pctx cited knowledge ledger with `pctx know add|retract|list|show|check`. Use when the user states a durable decision, preference, fact or procedure to remember, says "remember this", or asks what decisions are on record or whether they still hold.
---

# pctx know

A small ledger of durable decisions, each backed by a verbatim quote. Entries are superseded or retracted, never edited.

## Record
    pctx know add --kind decision|fact|preference|procedure \
      --text "short statement" --cite REF --quote "verbatim words" [--supersedes K] [--global]

- The quote must be verbatim in a primary prompt, reply or tool call at `REF`; a `preference` needs a user prompt. Find the ref with `pctx search`, confirm with `pctx open`, or test with `pctx quote-check REF QUOTE` first.
- Record owner decisions only when the owner actually said them. Do not invent or paraphrase the quote.
- `--global` for cross-repo entries; default is this repo.
- Check `know list` first so you supersede instead of duplicating.

## Read and maintain
- `pctx know list [--status current|superseded|retracted|erased|all] [--kind K] [--all-projects]`
- `pctx know show K` entry, supersede chain and log.
- `pctx know retract K --reason "why"` when a decision no longer stands.
- `pctx know check` re-verifies every citation (`ok`, `changed`, `missing`, `erased`).

`add` and `retract` take the writer lock: exit code 3 means busy, retry.
