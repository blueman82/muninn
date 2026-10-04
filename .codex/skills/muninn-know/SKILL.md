---
name: muninn-know
description: Manage the muninn cited knowledge ledger with `muninn know add|retract|list|show|check`. Use when the user states a durable decision, preference, fact or procedure to remember, says "remember this", or asks what decisions are on record or whether they still hold.
---

# muninn know

A small ledger of durable decisions, each backed by a verbatim quote. Entries are superseded or retracted, never edited.

## Record
    muninn know add --kind decision|fact|preference|procedure|lesson|constraint \
      --text "short statement" --cite REF --quote "verbatim words" [--supersedes K] [--global]

- The quote must be verbatim in a primary prompt, reply or tool call at `REF`; a `preference` needs a user prompt. Find the ref with `muninn search`, confirm with `muninn open`, or test with `muninn quote-check REF QUOTE` first.
- Record owner decisions only when the owner actually said them. Do not invent or paraphrase the quote.
- `--global` for cross-repo entries; default is this repo.
- Optional: `--confidence observed|reported|inferred`, `--valid-until DATE` (later it is expired, not current), `--sensitivity restricted` (never pushed by hooks), `--contradicts K` (informational), `--tag T` (repeatable), `--scope-loop ID`. A bad value is refused with a code such as `bad_confidence`.
- Check `know list` first so you supersede instead of duplicating.

## Read and maintain
- `muninn know list [--status current|superseded|retracted|erased|expired|all] [--kind K] [--all-projects] [--scope-loop ID]`
- `muninn know show K` entry, supersede chain and log.
- `muninn know retract K --reason "why"` when a decision no longer stands.
- `muninn know check` re-verifies every citation (`ok`, `changed`, `missing`, `erased`).

`add` and `retract` take the writer lock: exit code 3 means busy, retry.
