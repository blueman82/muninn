# muninn command skills

`.claude/skills/muninn-*` and `.codex/skills/muninn-*/SKILL.md` hold one
skill per user-facing command: `search`, `open`, `sessions` (also `session`),
`know`, `quote-check`, `stats`, `doctor`, `compact`, `ingest`, `erase` and
`rebuild`. They tell the model when to reach for a command and how to read its
answer (snippets are navigation; open before citing). `erase` and `rebuild`
are destructive: their Codex skills set `allow_implicit_invocation: false`, and
their Claude skills set `disable-model-invocation`, so only the owner can
invoke them. `serve` and `hook` have no skill because launchd and the providers
run them.

The skills load in sessions opened in this repository. The hook text
(`muninn/hook_frame.py`) deliberately does not mention them, so a machine
without the skills sees the same minimal frame. `docs/REFERENCE.md` stays the
source of truth for flags; update the matching Claude and Codex skills whenever
a command or flag changes.
