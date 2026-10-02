# AGENTS.md: pctx

Instructions for coding agents working in this repo. Any parent-directory
`AGENTS.md` defaults also apply.

## Layout

- `pctx/`: runtime package (Python 3.13 standard library only).
  `cli.py` (commands), `store.py` (schema, locks), `ingest.py`, `classify.py`,
  `query/` (package), `knowledge.py`, `erase.py`, `hook.py`, `obs.py` (stats, doctor,
  logs).
- `bin/pctx`: launcher that finds the interpreter; do not hardcode a path.
- `install/`: `installer.py` (`--fresh`, `--upgrade`),
  `rollback.py`, `configedit.py`.
- `integrations/`, `launchd/`: templates with a literal `@HOME@`.
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
