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
- Only entries whose `--text` stays close to the cited user prompt quote (inside it, or mostly its words) are pushed into session memory; otherwise `know add` answers `pull_only`: cite a user prompt, or re-add it in the user's own words. A `restricted` or expired entry also answers `pull_only`, with its own note: re-citing does not change that.
- Record owner decisions only when the owner actually said them. Do not invent or paraphrase the quote.
- `--global` for cross-repo entries; default is this repo.
- Optional: `--confidence observed|reported|inferred`, `--valid-until DATE` (ISO date or datetime, UTC if no zone, must be in the future; later it is expired, not current), `--sensitivity restricted` (never pushed by hooks), `--contradicts K` (informational), `--tag T` (repeatable; at most 10, counted before duplicates are dropped; stored sorted and unique), `--scope-loop ID` (a loop's own scope, never pushed by hooks). A bad value is refused with a code such as `bad_confidence`.
- Check `know list` first so you supersede instead of duplicating.

## Read and maintain
- `muninn know list [--status current|superseded|retracted|erased|expired|all] [--kind K] [--tag T]... [--all-projects] [--scope-loop ID]` (an unknown loop id lists 0 and sets `loop_not_found`); `--tag` is repeatable and an entry must carry every tag given (whole tags, so `run-1` never matches `run-10`; the count is after the filter; a malformed tag is refused with `bad_tags`)
- `muninn know show K` entry, supersede chain and log.
- `muninn know retract K --reason "why"` when a decision no longer stands.
- `muninn know check` re-verifies every citation (`ok`, `changed`, `missing`, `erased`) and counts `expired` entries.

`add` and `retract` take the writer lock: exit code 3 means busy, retry.
