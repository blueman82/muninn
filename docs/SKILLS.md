# Claude Code skills

`.claude/skills/muninn-*` holds one Claude Code skill per user-facing command:
`search`, `open`, `sessions` (also `session`), `know`, `quote-check`, `stats`,
`doctor`, `compact`, `ingest`, `erase` and `rebuild`. They tell the model when
to reach for the command and how to read its answer (snippets are navigation,
open before citing). `muninn-erase` and `muninn-rebuild` are destructive and set
`disable-model-invocation`, so only the owner can invoke them. `serve` and
`hook` have no skill because launchd and the providers run them.

The skills only load in Claude Code sessions opened in this repository.
Codex gets none, and the hook text (`muninn/hook_frame.py`) deliberately does not
mention them, so a machine without the skills sees the same minimal frame.
`docs/REFERENCE.md` stays the source of truth for flags; change the matching
skill whenever a command or flag changes.
