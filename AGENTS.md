# AGENTS.md: pctx

Instructions for coding agents working in this repo. Any parent-directory
`AGENTS.md` defaults also apply.

## Layout

- `pctx/`: runtime package (Python 3.13 standard library only), one
  responsibility per module; a feature's modules share its prefix:
  `cli*` (parser, handlers, output, poller), `store`, `ingest*` (plan, parse),
  `classify` with `claude_events`, `codex_events`, `event_model`,
  `tool_errors`, `redaction`; `query/` (package), `knowledge*`, `erase*`,
  `tombstones`, `hook*`, `obs*` (logs, stats, doctor, status), `scope`.
- `bin/pctx`: launcher that finds the interpreter; do not hardcode a path.
- `install/`: `installer.py` (`--fresh`, `--upgrade`; the runner),
  `steps_release.py`, `steps_config.py`, `verify.py`, `preflight.py`,
  `transforms.py`, `trust.py`, `rollback.py`, `configedit.py`, `tomledit.py`.
- `tools/`: the standards gate (`python3.13 -m tools.check`).
- `integrations/`, `launchd/`: templates with a literal `@HOME@`.
- `.claude/skills/pctx-*`: one Claude Code skill per user-facing command.
  When a command, flag or answer field changes, update the matching skill and
  `docs/REFERENCE.md` together.
- `docs/`: reference, quick start, troubleshooting, architecture, `adr/` (decisions).

## Rules

- No third-party imports in `pctx/` or `install/`.
- Logs and `doctor`/`stats` output hold counts, codes and ids, never
  transcript text. New log fields go through the allowlist in `obs.py`.
- Output is JSON on stdout; `--pretty` only changes indentation. Hooks must
  keep returning compact JSON and exit 0.
- Do not write to a user's real `HOME`, launchd domain or provider config in
  tests: use a temp `PCTX_HOME` and the fakes in `tests/test_installer.py`.
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
    python3.13 -m tools.run_tests          # the suite alone, in parallel (~8 s)
    python3.13 -m unittest discover -s tests -t .   # the suite, serial (~35 s)

Set up once: `python3.13 -m venv .venv && .venv/bin/pip install -r
requirements-dev.txt` and `git config core.hooksPath .githooks`. The pre-commit
and pre-merge-commit hooks and `tests/test_standards.py` run the whole gate;
the installer runs only the standards rules (S1 to S8), not ruff, pyright or the
tests; Claude Code's Stop hook (`.claude/settings.json`) blocks stopping on a
failure. A missing tool or a failing test fails the gate; it never skips.

## Decisions you must not reverse without asking

`docs/adr/`: one pinned release and in-place upgrade (0001); the installer has
only `--fresh` and `--upgrade` (0002); `bin/pctx` finds its interpreter at run
time (0003); logs hold no transcript text (0004); no hash-chained call log
(0005); the standards are enforced by a gate (0006); per-prompt recall starts
off on a fresh install (0007).

## Definition of done (Codex has no Stop hook, so this is on you)

- Work is not done until `python3.13 -m tools.check --full` passes on the final
  tree, run after your last edit. If another agent changes a checked file
  afterwards, your result is void: run it again.
- Report a failing gate as unfinished work with the failing lines, never as
  done. State the gate's result verbatim in your final message.
- Your sandbox may not allow writes under `.git`, so you may be unable to
  commit or set hooks. Then leave the changes uncommitted, say so, and let the
  owner or the Claude session commit them; the git hook is the enforcement
  point. Do not try to edit `.git/config` or hooks yourself.
- If `git status` says "must be run in a work tree", the repo's `core.bare`
  was flipped to true by something. Stop and tell the owner.

## Verify a change

Run the narrowest tests first, then the full suite. Say what was run and what
was not (a live launchd run and a real colleague install are manual checks).
