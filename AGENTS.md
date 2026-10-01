# AGENTS.md: pctx

Instructions for coding agents working in this repo. Any parent-directory
`AGENTS.md` defaults also apply.

## Layout

- `pctx/`: runtime package (Python 3.13 standard library only).
  `cli.py` (commands), `store.py` (schema, locks), `ingest.py`, `classify.py`,
  `query.py`, `knowledge.py`, `erase.py`, `hook.py`, `obs.py` (stats, doctor,
  logs).
- `bin/pctx`: launcher that finds the interpreter; do not hardcode a path.
- `install/`: `installer.py` (`--fresh`, `--upgrade`),
  `rollback.py`, `configedit.py`.
- `integrations/`, `launchd/`: templates with a literal `@HOME@`.
- `trial_harness/`: the evaluation driver for this machine only; not runtime.
- `docs/`: quick start, troubleshooting, architecture.

## Rules

- No third-party imports in `pctx/` or `install/`.
- Logs and `doctor`/`stats` output hold counts, codes and ids, never
  transcript text. New log fields go through the allowlist in `obs.py`.
- Output is JSON on stdout; `--pretty` only changes indentation. Hooks must
  keep returning compact JSON and exit 0.
- Do not write to a user's real `HOME`, launchd domain or provider config in
  tests: use a temp `PCTX_HOME` and the fakes in `tests/test_installer.py`.
- Never touch provider transcripts.

## Check

    python3.13 -m unittest discover -s tests -t .

Style (from a venv with ruff, black, isort): `ruff check .`,
`black --check .`, `isort --check-only .`.

## Verify a change

Run the narrowest tests first, then the full suite. Say what was run and what
was not (a live launchd run and a real colleague install are manual checks).
