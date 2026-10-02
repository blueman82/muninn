# 0007: Per-prompt recall starts off on a fresh install

Status: Accepted, 2026-10-02. Decided by: owner. Supersedes the earlier default (on) described in `docs/REFERENCE.md`.

**Context.** The UserPromptSubmit hook can add up to 1,500 characters of earlier prompts and replies to every prompt
(`pctx hook prompt`). It is the only memory pctx pushes without being asked. Before cutover, a pre-registered trial of the
new system compared it with the old memory tool on 40 questions. Per the trial session's report, in 12 of the deliberate
"wrong project" control questions the new system answered when it should have said "not this project", and it scored 8 fully
correct answers against the old tool's 12 (p = 0.34 on 28 usable pairs, not statistically meaningful). Under the pre-set rule
the cutover was held on that safety finding. Retrieval and answer-quality re-checks are still the next product work
(`HANDOFF.md`).

**Decision.** `install --fresh` creates `recall.off` (mode 0600) in the data directory, so a new machine starts with
per-prompt recall off. SessionStart memory stays on. `--upgrade` never creates or removes the file, so an owner's choice
survives upgrades. The installer prints why recall is off and the one command that turns it on.

**Reasoning.** The trial measured answering, not the hook itself, so the link is a judgment: recall pushes earlier text into a
prompt with nobody asking, which is where an answer about the wrong project costs the most. Off is the cheap, reversible
side while that is unproven. Search and `open` still work on demand.

**Consequences.**
- A new machine gets no per-prompt recall until someone runs `unlink ~/.local/share/provenance-context/recall.off`.
- A colleague who never reads the installer output sees recall as missing, not broken. `docs/QUICKSTART.md` and
  `docs/REFERENCE.md` carry the same switch instructions.
- Turn the default back on (stop creating the file in `install/steps_release.py: ingest_fresh`) once the retrieval re-check
  shows wrong-project answers are fixed, then supersede this record.

**Where the reasoning is recorded (owner's pctx history).** Run `pctx open REF --context 3` on:
- `claude:1a23fdfe-87cc-4053-869b-3145ef08c81c:3919.1`: the trial finding and the hold recommendation.
- `claude:14777623-5745-49cb-a188-7401fc24c182:461.1`: the recommendation to keep the `recall.off` switch.
These refs resolve only in the owner's database; the numbers above are what they say.
