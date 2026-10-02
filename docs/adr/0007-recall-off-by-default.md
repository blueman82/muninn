# 0007: Per-prompt recall starts off on a fresh install

Status: Accepted, 2026-10-02. Decided by: owner.

**Context.** The UserPromptSubmit hook can add up to 1,500 characters of earlier prompts and replies to every prompt
(`pctx hook prompt`). It is the only memory pctx pushes without being asked. Whether the text it picks is about the current
project has not been re-checked.

**Decision.** `install --fresh` creates `recall.off` (mode 0600) in the data directory, so a new machine starts with
per-prompt recall off. SessionStart memory stays on. `--upgrade` never creates or removes the file, so an owner's choice
survives upgrades. The installer prints why recall is off and the one command that turns it on.

**Reasoning.** Recall pushes earlier text into a prompt with nobody asking, which is where an answer about the wrong
project costs the most. Off is the cheap, reversible side while its relevance is unproven. Search and `open` still work on
demand.

**Consequences.**
- A new machine gets no per-prompt recall until someone runs `unlink ~/.local/share/provenance-context/recall.off`.
- A colleague who never reads the installer output sees recall as missing, not broken. `docs/QUICKSTART.md` and
  `docs/REFERENCE.md` carry the same switch instructions.
- Turn the default back on (stop creating the file in `install/steps_release.py: ingest_fresh`) once the retrieval re-check
  shows recall stays within the current project, then supersede this record.
