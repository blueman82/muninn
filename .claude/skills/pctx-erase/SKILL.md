---
name: pctx-erase
description: Forget stored content with `pctx erase`. Use only when the user explicitly asks to erase, forget or remove a session, event or text match from pctx memory (for example a leaked secret). Never run it on your own initiative.
disable-model-invocation: true
---

# pctx erase

    pctx erase --session S | --event REF | --match TEXT            # dry run (default)
    pctx erase --session S | --event REF | --match TEXT --yes      # actually erase

Why the care: erase is permanent. It secure-deletes rows and full-text entries and writes tombstones (ids and hashes only) so a rescan cannot bring the content back.

## Procedure
1. Run the dry run first and show the user the targets it lists. Do not add `--yes` yet.
2. Get an explicit yes from the user for exactly that target set. A broad `--match` can hit more than expected.
3. Re-run with `--yes`.
4. Report the answer's `not_covered` and `out_of_scope` lists: provider transcript files, Time Machine and free disk blocks are outside pctx's reach. Tell the user the provider file paths if they want those removed too. Do not delete provider transcripts yourself.
5. Run `pctx doctor` afterwards.

Takes the writer lock: exit code 3 means busy, retry.
