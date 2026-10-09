---
name: muninn-erase
description: Forget stored content with `muninn erase`. Use only when the user explicitly asks to erase, forget or remove a session, event or text match from muninn memory (for example a leaked secret). Never run it on your own initiative.
allow_implicit_invocation: false
---

# muninn erase

    muninn erase --session S | --event REF | --match TEXT            # dry run (default)
    muninn erase --session S | --event REF | --match TEXT --yes      # actually erase

Erasure is permanent. It secure-deletes rows and full-text entries and writes tombstones (ids and hashes only) to prevent reimport, including from fork copies. `--match` searches event text, knowledge text, tags, retract reasons and citation quotes.

## Procedure

1. Run the dry run first and show the user the targets it lists. Do not add `--yes` yet.
2. Obtain explicit user approval for that exact target set. A broad `--match` may include unintended targets.
3. Re-run with `--yes`.
4. Report `not_covered` and `out_of_scope`: muninn does not erase provider transcript files, Time Machine backups or free disk blocks. Provide provider file paths if the user wants to remove those files. Do not delete provider transcripts yourself. If `aside_files` is not empty, those stores may still contain erased text. Show the user `aside_remove` (`rm --` on macOS/Linux, or PowerShell `Remove-Item -LiteralPath` with a literal path array on Windows) for manual execution; do not delete these stores yourself.
5. Run `muninn doctor` afterwards.

Takes the writer lock: exit code 3 means busy, retry.
