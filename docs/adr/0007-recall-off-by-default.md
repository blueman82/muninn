# 0007: Per-prompt recall starts off on a fresh install

## Status

Accepted

Date: 2026-10-02

## Context

The UserPromptSubmit hook can add up to 1,500 characters of earlier prompts and replies
to every prompt (`muninn hook prompt`). Recall automatically selects earlier event text
for each prompt. Its relevance to the current project has not been revalidated.

## Decision

`install --fresh` creates `recall.off` (mode 0600) in the data directory, so a new
machine starts with per-prompt recall off. SessionStart memory stays on. `--upgrade`
never creates or removes the file, so the existing setting survives upgrades. The
installer prints why recall is off and the one command that turns it on.

## Rationale

Automatic insertion can introduce irrelevant material from another project. Disabling
recall is reversible and avoids that risk while retrieval relevance remains unverified.
Search and `open` remain available on demand.

## Consequences

- A new machine gets no per-prompt recall until someone runs
  `unlink ~/.local/share/muninn/recall.off`.
- Users who miss the installer output may not realize recall is disabled.
  `docs/QUICKSTART.md` and `docs/REFERENCE.md` carry the same switch instructions.
- Re-enabling the default requires retrieval validation showing that recall stays within
  the current project. Supersede this record before removing creation of `recall.off`
  from `install/steps_release.py: ingest_fresh`.
