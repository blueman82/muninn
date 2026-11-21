---
name: muninn-erase
description: Forget stored content with `muninn erase`. Use only when the user explicitly asks to erase, forget or remove a session, event or text match from muninn memory (for example a leaked secret). Never run it on your own initiative.
disable-model-invocation: true
---

# muninn erase

Preview is the default:

```sh
muninn erase --session S | --event REF | --match TEXT
```

Apply the specified erasure:

```sh
muninn erase --session S | --event REF | --match TEXT --yes
```

Erasure is permanent.
It secure-deletes rows and full-text entries.
It writes tombstones with IDs and hashes only to prevent reimport from sources and fork copies.
`--match` searches `event` text, governed knowledge, `reason` values, expiry conditions, claims, `evidence`, and unresolved content.

Erasure follows prior revisions and `support`/read links.
It removes affected `guides` from current use and fences `review` `requests`.
It clears candidate payloads and each affected decision locator, with pending intents before `evidence` admission.
Accepted revision metadata and historical receipts stay, but cannot put an erased body back.

Current `evidence` keeps checked source-path and selector metadata.
Do not claim that all possible provenance strings are content-free.

## Procedure

1. Run the dry run and show the listed targets.

2. Get the user's approval for that unchanged target set.

3. If `--match` is broad, warn that it can select unintended targets.

4. After approval, run the same command with `--yes`.

5. Report `not_covered` and `out_of_scope`.

6. Also report `vocab`, `residue`, and `review.retained_archives` if the result has them.

7. Report that Muninn does not erase provider transcripts, Time Machine recovery copies, or free disk blocks.

8. If the user wants provider transcript files removed, give their paths.

9. Do not remove provider transcripts.

10. If `aside_files` is not empty, warn that those databases can contain erased text.

11. Show the `aside_remove` command for manual use.
    It is `rm --` with the listed literal paths.

12. Tell the user to remove aside databases.

13. Do not remove aside databases.

14. Run `muninn doctor --pretty` after erasure.

Erasure takes the writer lock.
A poller pass in progress makes it exit 3 (busy).
Dry runs can make an empty database when no database is on disk.
Live erasure of a missing database refuses with exit 4 and `store_unavailable`, without making one.

A completed command with `residue` exits 0.
These bounded checks do not prove that all forensic signs are gone.
They do not erase provider or remote copies.
Tombstones are written before any content is scrubbed.
They replay first on the next start.
Keep the journal `tombstones.jsonl` and `tombstone.key`.

Exits stay 3 for busy.
They stay 4 for hot journals, tombstone keys, and unavailable databases.
They stay 2 for bad arguments.
