# AGENTS.md: muninn

Instructions for coding agents working in this repo. Any parent-directory
`AGENTS.md` defaults also apply.

## Layout

- `muninn/`: runtime package (Python 3.13 standard library only), one
  responsibility per module; a feature's modules share its prefix:
  `cli*` (parser, handlers, output, poller, `cli_rebuild`), `store*` (`store_schema`, `store_migrate`),
  `ingest*` (plan, parse, lines, model),
  `classify` with `claude_events`, `codex_events`, `event_model`,
  `tool_errors`, `redaction`; `query/` (package), `knowledge*` (with `knowledge_typed`, `knowledge_expiry`), `erase*` (`erase_collect`, `erase_family`, `erase_residue`),
  `tombstones`, `tombstone_key` (the HMAC key for content tombstones), `hook*` (`hook_frame`, `hook_notes`,
  `hook_recall`), `obs*` (`obs_log`, `obs_stats`, `obs_status`; `obs` holds doctor), `scope`.
- `bin/muninn`: launcher that finds the interpreter; do not hardcode a path.
- `bin/muninn-install`: sh wrapper over `install/installer.py` (bare, `--check`,
  `--status`, `--uninstall`); other arguments pass through.
- `bin/muninn-uninstall`: names `bin/muninn-install --uninstall`
  (`--dry-run`, `--purge-data`). Agents run only `--dry-run`.
- `install/`: `installer.py` (`--fresh`, `--upgrade`; the runner),
  `steps_release.py`, `steps_config.py`, `verify.py`, `preflight.py`,
  `transforms.py`, `trust.py`, `rollback.py`, `snapshot.py` (pre-upgrade store copy), `record.py`,
  `probe.py`, `context.py`, `errors.py`, `constants.py`, `uninstall.py`, `configedit.py`,
  `tomledit.py`; `ls install` is the full list.
- `tools/`: the standards gate (`python3.13 -m tools.check`).
- `integrations/`, `launchd/`: templates with a literal `@HOME@`.
- `.claude/skills/muninn-*` and the mirrors `.codex/skills/muninn-*`: one skill per
  user-facing command. When a command, flag or answer field changes, update both skills and
  `docs/REFERENCE.md` together (`docs/SKILLS.md`).
- `docs/`: reference, quick start, troubleshooting, architecture, `adr/` (decisions).

## Rules

- No third-party imports in `muninn/` or `install/`.
- Logs and `doctor`/`stats` output hold counts, codes and ids, never
  transcript text. New log fields go through the allowlist in `muninn/obs_log.py`.
- Output is JSON on stdout; `--pretty` only changes indentation. muninn provider
  hooks must keep returning compact JSON and exit 0.
- Do not write to a user's real `HOME`, launchd domain or provider config in
  tests: use a temp `MUNINN_HOME` and the fakes in `tests/test_installer.py`.
- Never touch provider transcripts.

## Standards: enforced by code, read `docs/STANDARDS.md`

- Every `.py` file has at most 400 non-blank lines and every function at most
  100; modules, classes and functions have Google-style docstrings; every
  parameter and return is annotated (pyright strict); comments explain why and
  never cite a design, spec or work-unit label; imports are explicit and at
  the top; ruff and black are clean.
- The only exemptions are the ones the checker encodes: `test_*` methods and
  fixture hooks (`setUp`, `tearDown`) need no docstring, closures (functions
  nested in functions) need none, and tests use pyright standard mode (every
  annotation is still required). There is no `noqa`, no `# type: ignore` and
  no other exemption. If a rule is wrong, change `tools/` or `pyproject.toml`
  with an ADR; do not work around it.
- A skill or guideline that says otherwise (for example "public docstrings
  only") does not override `docs/STANDARDS.md`.

## Check

    python3.13 -m tools.check --full       # the whole gate: standards, ruff, black,
                                           # pyright strict, shellcheck, hooks, tests
    python3.13 -m tools.run_tests          # the suite alone, in parallel (roughly 9 s)
    python3.13 -m unittest discover -s tests -t .   # the suite, serial (roughly 42 s)

Set up once: `python3.13 -m venv .venv && .venv/bin/pip install -r
requirements-dev.txt` and `git config core.hooksPath .githooks`. If Codex cannot
write Git metadata, the owner or Claude session configures the Git hooks.
The pre-commit and pre-merge-commit hooks run the whole gate;
`tests/test_standards.py` runs the stdlib rules and checks enforcement
configuration;
the installer runs only the standards rules (S1 to S8), not ruff, pyright or the
tests; Claude Code's Stop hook (`.claude/settings.json`) blocks stopping on a
failure. Codex's project Stop hook (`.codex/hooks.json`) runs the full gate
through `tools/codex_stop_gate.py`. The owner must trust the project and approve
that exact hook definition in Codex CLI `/hooks`; changed definitions need
approval again. Codex hooks are enabled by default (`features.hooks = true`);
if disabled, the Stop gate cannot run. See `docs/STANDARDS.md` for its contract.
A missing tool or a failing test fails the gate; it never skips.

## Decisions you must not reverse without asking

`docs/adr/`: one pinned release and in-place upgrade (0001); the installer has
only `--fresh` and `--upgrade` (0002); `bin/muninn` finds its interpreter at run
time (0003); logs hold no transcript text (0004); no hash-chained call log
(0005); the standards are enforced by a gate (0006); per-prompt recall starts
off on a fresh install (0007); `bin/muninn-install` picks the mode and `--check`
writes nothing (0008); `bin/muninn-uninstall` keeps the data unless told
otherwise (0009); Codex ChatGPT-handoff sessions are primary (0010); the schema
migration is one-way and an upgrade snapshots the store first (0011); content
tombstones are keyed HMAC tags and `tombstone.key` is a durable secret (0012).

## Definition of done (Codex Stop hook plus manual fallback)

- Work is not done until `python3.13 -m tools.check --full` passes on the final
  tree, run after your last edit. If another agent changes a checked file
  afterwards, your result is void: run it again.
- Report a failing gate as unfinished work with the failing lines, never as
  done. State the gate's result verbatim in your final message.
- The trusted, approved Codex Stop hook blocks completion on failure. If hooks
  are disabled, unapproved or unavailable in the current host, run the full
  gate manually: the same definition of done applies. A repeated Stop never
  bypasses a failure; fix its cause or ask the owner for help before retrying.
- Your sandbox may not allow writes under `.git`, so you may be unable to
  commit or set hooks. Then leave the changes uncommitted, say so, and let the
  owner or the Claude session commit them; the git hook is the enforcement
  point. Do not try to edit `.git/config` or hooks yourself.
- If `git status` says "must be run in a work tree", the repo's `core.bare`
  was flipped to true by something. Stop and tell the owner.

## Verify a change

Run the narrowest tests first, then the full suite. Say what was run and what
was not (a live launchd run and a real colleague install are manual checks).
