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
- There is no `noqa`, no `# type: ignore` and no exemption list. If a rule is
  wrong, change `tools/` or `pyproject.toml` with an ADR; do not work around it.
- A skill or guideline that says otherwise (for example "public docstrings
  only") does not override `docs/STANDARDS.md`.

## Check

    python3.13 -m unittest discover -s tests -t .
    python3.13 -m tools.check --full       # the whole gate

Set up the gate tools once:
`python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
and `git config core.hooksPath .githooks`. The pre-commit hook, the test suite,
Claude Code's Stop hook and the installer all run the gate; a missing tool
fails it.

## Verify a change

Run the narrowest tests first, then the full suite. Say what was run and what
was not (a live launchd run and a real colleague install are manual checks).
