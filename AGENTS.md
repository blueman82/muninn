# Agent instructions

Instructions for coding agents working in this repository. Any parent-directory
`AGENTS.md` defaults also apply.

## Layout

- `muninn/`: runtime package (Python 3.13+ with the pinned validation closure), one responsibility per
  module; related modules share a prefix: `cli*` (parser, handlers, output, poller,
  `cli_rebuild`), `store*` (`store_schema`, `store_copy`, `store_fingerprint`), `ingest*` (plan, parse,
  lines, model), `classify` with `claude_events`, `codex_events`, `event_model`,
  `tool_errors`, `redaction`; `query/` (package), `knowledge*` (`knowledge_models`,
  `knowledge_read`, `knowledge_write`, `knowledge_push`, `knowledge_erase`), `erase*` (`erase_collect`, `erase_family`, `erase_residue`),
  `tombstones`, `tombstone_key` (the HMAC key for content tombstones), `hook*`
  (`hook_frame`, `hook_notes`, `hook_recall`), `obs*` (`obs_log`, `obs_stats`,
  `obs_status`; `obs` holds doctor), `scope`.
- `bin/muninn`: launcher that finds the interpreter; do not hardcode a path.
- `bin/muninn-install`: sh wrapper over `install/installer.py` (bare, `--check`,
  `--status`, `--uninstall`); other arguments pass through.
- `bin/muninn-uninstall`: invokes `bin/muninn-install --uninstall` (`--dry-run`,
  `--purge-data`). Agents run only `--dry-run`.
- `install/`: `installer.py` (`--fresh`, `--upgrade`; orchestration),
  `steps_release.py`, `steps_config.py`, `verify.py`, `preflight.py`, `transforms.py`,
  `trust.py`, `rollback.py`, `snapshot.py` (pre-upgrade store copy), `record.py`,
  `probe.py`, `context.py`, `errors.py`, `constants.py`, `uninstall.py`,
  `configedit.py`, `tomledit.py`; the directory contains the complete module list.
- `tools/`: the standards gate (`python3.13 -m tools.check`) and optional
  offline replay tool (`tools.replay_retrieval`).
- `integrations/`, `launchd/`: templates with a literal `@HOME@`. Linux and Windows
  services are rendered in `install/lifecycle.py`.
- `.claude/skills/muninn-*` and the mirrors `.codex/skills/muninn-*`: one skill per
  user-facing command. When a command, flag or answer field changes, update both skills
  and `docs/REFERENCE.md` together.
- `docs/`: reference, quick start, troubleshooting, architecture, `adr/` (decisions).

## Rules

- Third-party imports in `muninn/` and `install/` are restricted to `pydantic`,
  `pydantic_core`, `annotated_types`, `typing_extensions` and `typing_inspection`,
  pinned with verified wheel hashes in `requirements-runtime.lock` (ADR 0014).
- Logs and `doctor`/`stats` output hold counts, codes and ids, never transcript text.
  New log fields go through the allowlist in `muninn/obs_log.py`.
- Output is JSON on stdout; `--pretty` only changes indentation. muninn provider hooks
  must keep returning compact JSON and exit 0.
- Do not write to a user's real `HOME`, launchd domain or provider config in tests: use
  a temp `MUNINN_HOME` and the fakes in `tests/test_installer.py`.
- Never touch provider transcripts.

## Engineering standards

`docs/STANDARDS.md` defines the enforced rules.

- Every `.py` file has at most 400 non-blank lines and every function at most 100;
  modules, classes and functions have Google-style docstrings; every parameter and
  return is annotated (pyright strict); comments explain why and never cite a design,
  spec or work-unit label; imports are explicit and at the top; ruff and black are
  clean.
- Only exemptions encoded by the checker apply: `test_*` methods and fixture hooks
  (`setUp`, `tearDown`) need no docstring, closures (functions nested in functions) need
  none, and tests use pyright standard mode (every annotation is still required). There
  is no `noqa`, no `# type: ignore` and no other exemption. If a rule is wrong, change
  `tools/` or `pyproject.toml` with an ADR; do not work around it.
- A skill or guideline that says otherwise (for example "public docstrings only") does
  not override `docs/STANDARDS.md`.

## Check

    .venv/bin/python3.13 -m tools.check --full
    .venv/bin/python3.13 -m tools.run_tests [-j N]
    .venv/bin/python3.13 -m unittest tests.test_module
    .venv/bin/python3.13 -m unittest discover -s tests -t .

Use the checkout's virtual-environment interpreter for every command. The full
gate and test suite use the pinned runtime packages. The test runner starts one
process per module; `-j N` sets the maximum number of modules it runs at once.
Run one module by name to narrow a failure.

Set up once, installing the hashed runtime separately from developer tools:

```sh
python3.13 -m venv .venv
.venv/bin/python3.13 -m pip install --require-hashes -r requirements-runtime.lock
.venv/bin/python3.13 -m pip install -r requirements-dev.txt
source .venv/bin/activate
```

On Windows create the same venv with the selected Python 3.13 interpreter and
use `.\.venv\Scripts\python.exe` in both pip commands and every later test/gate
command. Explicit interpreter paths require no shell activation.

The owner configures `git config core.hooksPath .githooks`. If Codex cannot write
Git metadata, the owner or Claude session configures the Git hooks. The pre-commit
and pre-merge-commit hooks
run the whole gate; `tests/test_standards.py` runs the stdlib rules and checks
enforcement configuration; the installer runs only the standards rules (S1 to S8), not
ruff, pyright or the tests; Claude Code's Stop hook (`.claude/settings.json`) blocks
stopping on a failure. Codex's project Stop hook (`.codex/hooks.json`) runs the full
gate through `tools/codex_stop_gate.py`. The owner must trust the project and approve
that exact hook definition in Codex CLI `/hooks`; changed definitions need approval
again. Codex hooks are enabled by default (`features.hooks = true`); if disabled, the
Stop gate cannot run. See `docs/STANDARDS.md` for its contract. A missing tool or a
failing test fails the gate; it never skips.

## Decisions requiring approval to change

`docs/adr/`: one pinned release and in-place upgrade (0001); the installer has only
`--fresh` and `--upgrade` (0002); `bin/muninn` finds its interpreter at run time (0003);
logs hold no transcript text (0004); no hash-chained call log (0005); the standards are
enforced by a gate (0006); per-prompt recall starts off on a fresh install (0007);
`bin/muninn-install` picks the mode and `--check` writes nothing (0008);
`bin/muninn-uninstall` keeps the data unless told otherwise (0009); Codex
ChatGPT-handoff sessions are primary (0010); the schema migration is one-way and an
upgrade snapshots the store first (0011); content tombstones are keyed HMAC tags and
`tombstone.key` is a durable secret (0012); Cursor import is read-only during full
ingest, without polling (0013); pinned Pydantic validation and the distinct new
knowledge format (0014).

## Definition of done (Codex Stop hook plus manual fallback)

- Work is not done until `python3.13 -m tools.check --full` passes on the final tree,
  run after your last edit. If another agent changes a checked file afterwards, your
  result is void: run it again.
- Report a failing gate as unfinished work with the failing lines, never as done. State
  the gate's result verbatim in your final message.
- The trusted, approved Codex Stop hook blocks completion on failure. If hooks are
  disabled, unapproved or unavailable in the current host, run the full gate manually:
  the same definition of done applies. A repeated Stop never bypasses a failure; fix its
  cause or ask the owner for help before retrying.
- Your sandbox may not allow writes under `.git`, so you may be unable to commit or set
  hooks. Then leave the changes uncommitted, say so, and let the authorized operator
  commit them; the Git hook remains the enforcement point. Do not try to edit
  `.git/config` or hooks yourself.
- If `git status` says "must be run in a work tree", the repo's `core.bare` was flipped
  to true by something. Stop and tell the owner.

## Verify a change

Run the narrowest tests first, then the full suite. Report executed checks and
verification limits. Live launchd behavior and installation on another physical machine
require separate manual verification.
