---
name: muninn-know
description: Manage the muninn cited knowledge ledger with `muninn know add|retract|list|show|check`. Use when the user states a durable decision, preference, fact or procedure to remember, says "remember this", or asks what decisions are on record or whether they still hold.
---

# muninn know

The ledger stores durable decisions backed by verbatim quotes. Supersede or retract entries; do not edit them.

## Record

    muninn know add --kind decision|fact|preference|procedure|lesson|constraint \
      --text "short statement" --cite REF --quote "verbatim words" [--supersedes K] [--global]

- The quote must be verbatim in a primary prompt, reply or tool call at `REF`; a `preference` needs a user prompt. Find the ref with `muninn search`, confirm with `muninn open`, or test with `muninn quote-check REF QUOTE` first.
- Only entries whose `--text` stays close to the cited user prompt quote (inside it, or mostly its words) are pushed into session memory; otherwise `know add` answers `pull_only`: cite a user prompt, or re-add it in the user's own words. A `restricted` or expired entry also answers `pull_only`, with its own note: re-citing does not change that.
- Record owner decisions only when stated by the owner. Do not invent or paraphrase the quote.
- `--global` records cross-repository entries; the default scope is this repository.
- Check `know list` first. Supersede existing entries instead of duplicating them.

## Optional fields

- `--confidence observed|reported|inferred`: confidence label.
- `--valid-until DATE`: future ISO date or datetime; UTC if no zone. After this time, the entry is expired rather than current.
- `--sensitivity restricted`: never pushed by hooks.
- `--contradicts K`: informational link.
- `--tag T`: repeatable; at most 10, counted before deduplication. Stored sorted and unique.
- `--scope-loop ID`: loop-specific scope; never pushed by hooks.

Invalid values are refused with a code such as `bad_confidence`.

## Read and maintain

- `muninn know list [--status current|superseded|retracted|erased|expired|all] [--kind K] [--tag T]... [--all-projects] [--scope-loop ID]` (an unknown loop id lists 0 and sets `loop_not_found`); `--tag` is repeatable and an entry must carry every tag given (exact tag matches; `run-1` does not match `run-10`; the count is after the filter; a malformed tag is refused with `bad_tags`)
- `muninn know show K` returns the entry, supersede chain and log.
- `muninn know retract K --reason "why"` when a decision no longer stands.
- `muninn know check` re-verifies every citation (`ok`, `changed`, `missing`, `erased`) and counts `expired` entries.

`add` and `retract` take the writer lock: exit code 3 means busy, retry.
