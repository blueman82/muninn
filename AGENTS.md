# Agent instructions

These are instructions for coding agents that work in this repository. Also use the defaults of the `AGENTS.md` in a parent directory.

## Layout

- `muninn/` is the runtime package (Python 3.13+ with the pinned validation closure). Each module has one purpose.

- Related modules share a prefix. `cli*` has the parser, handlers, output, poller and `cli_rebuild`. `store*` has `store_schema`, `store_copy` and `store_fingerprint`.

- `ingest*` has plan, parse, lines and model. `classify` works with `claude_events`, `codex_events`, `event_model`, `tool_errors` and `redaction`.

- `query/` is a package. `knowledge*` has `knowledge_models`, `knowledge_read`, `knowledge_write`, `knowledge_push` and `knowledge_erase`.

- `erase*` has `erase_collect`, `erase_family` and `erase_residue`. `tombstones` and `tombstone_key` handle tombstones. `tombstone_key` holds the HMAC key for content tombstones.

- `hook*` has `hook_frame`, `hook_notes` and `hook_recall`. `obs*` has `obs_log`, `obs_stats` and `obs_status`. `obs` holds doctor. `scope` maps a directory to a scope.

- `bin/muninn` is a launcher that finds the interpreter. Do not write a path in the code.

- `bin/muninn-install` is an sh wrapper for `install/installer.py`. It accepts bare, `--check`, `--status` and `--uninstall`. Other arguments pass through.

- `bin/muninn-uninstall` calls `bin/muninn-install --uninstall`. It accepts `--dry-run` and `--purge-data`. Agents run only `--dry-run`.

- `install/` holds `installer.py` (`--fresh` and `--upgrade`, and the orchestration). It also holds `steps_release.py`, `steps_config.py`, `verify.py`, `preflight.py` and `transforms.py`.

- `install/` also holds `trust.py`, `rollback.py`, `record.py`, `probe.py`, `context.py`, `errors.py` and `constants.py`.

- `snapshot.py` in `install/` makes a copy of the store before an upgrade.

- `install/` also holds `uninstall.py`, `configedit.py`, `tomledit.py` and `recovery_guidance.py`. The directory has the full module list.

- `tools/` holds the standards gate (`python3.13 -m tools.check`). It also holds an optional offline replay tool (`tools.replay_retrieval`).

- `integrations/` and `launchd/` hold templates with a literal `@HOME@`. `install/lifecycle.py` makes the Linux and Windows services.

- `.claude/skills/muninn-*` and the copies in `.codex/skills/muninn-*` hold one skill for each user command.

- A command, flag or answer field can change. Then update the two skills and `docs/REFERENCE.md` together.

- `docs/` holds the manual, the start guide, troubleshooting, architecture and `adr/` (the decisions).

## Rules

- `muninn/` and `install/` can import only these third-party packages: `pydantic`, `pydantic_core`, `annotated_types`, `typing_extensions` and `typing_inspection`.

- The file `requirements-runtime.lock` pins them with checked wheel hashes (ADR 0014).

- Logs and the output of `doctor` and `stats` hold counts, codes and ids. They do not hold transcript text.

- New log fields go through the allowlist in `muninn/obs_log.py`.

- Output is JSON on stdout. `--pretty` changes only the indentation. The muninn provider hooks must keep a compact JSON output and exit 0.

- Do not write to the `HOME`, launchd domain or provider config of a user in tests. Use a temp `MUNINN_HOME` and the fakes in `tests/test_installer.py`.

- Do not touch provider transcripts.

## Engineering standards

`docs/STANDARDS.md` defines the enforced rules.

- Each `.py` file has 400 non-blank lines or less. Each function has 100 lines or less.

- Modules, classes and functions have Google-style docstrings. Each parameter and each result has a type (pyright strict).

- Comments explain why. They do not give a design, spec or work-unit label.

- Imports are explicit and at the top. Ruff and black give no errors.

- The checker has a fixed list of exemptions. `test_*` methods and fixture hooks (`setUp` and `tearDown`) have no docstring. Closures (functions in functions) have none.

- Tests use pyright standard mode, but each type is necessary. There is no `noqa`, no `# type: ignore` and no other exemption.

- A rule can be incorrect. Then change `tools/` or `pyproject.toml` with an ADR. Do not work around the rule.

- A skill or guideline can say something different, for example "public docstrings only". It does not override `docs/STANDARDS.md`.

## Check

    .venv/bin/python3.13 -m tools.check --full
    .venv/bin/python3.13 -m tools.run_tests
    .venv/bin/python3.13 -m tools.run_tests -j 4
    .venv/bin/python3.13 -m unittest tests.test_module
    .venv/bin/python3.13 -m unittest discover -s tests -t .

Use the `.venv` Python of this checkout for the commands above. The Codex Stop hook first checks the `.venv` of the current checkout.

A linked worktree can have no complete `.venv`. Then the hook checks the `.venv` of the primary checkout. If no checkout has a complete `.venv`, the hook reports a launch error.

The full gate and the test suite use the pinned runtime packages. The runner starts one procedure for each module. It uses four workers or less by default.

The `-j N` option sets the worker limit. Run one module by name to find a smaller error. Each module has a deadline of 600 seconds.

A direct runner call can set `--timeout SECONDS` to a finite positive value. A timeout reports the module and keeps the output so far. It also stops the run. Cleanup can use five more seconds.

Ctrl-C cancels the queued work and stops the active module procedures.

Set up one time. Install the hashed runtime and the developer tools in two steps:

```sh
python3.13 -m venv .venv
.venv/bin/python3.13 -m pip install --require-hashes -r requirements-runtime.lock
.venv/bin/python3.13 -m pip install --require-hashes -r requirements-dev.txt
source .venv/bin/activate
```

On Windows, make the same venv with the selected Python 3.13 interpreter. Use `.\.venv\Scripts\python.exe` in the two pip commands and in all test and gate commands that follow. An explicit interpreter path needs no shell activation.

The owner sets `git config core.hooksPath .githooks`. If Codex cannot write Git metadata, the owner or the Claude session sets the Git hooks.

The pre-commit and pre-merge-commit hooks run the full gate. `tests/test_standards.py` runs the stdlib rules and checks the enforcement settings.

The installer runs only the standards rules (S1 to S8). It does not run ruff, pyright or the tests.

The PostToolUse hook of Claude Code (`.claude/settings.json`) runs the fast check after each edit. The project Stop hook of Codex (`.codex/hooks.json`) runs the full gate with `tools/codex_stop_gate.py`.

The owner must trust the project. The owner must give approval for that hook definition in Codex CLI `/hooks`. A changed definition needs a new approval.

Codex hooks are on by default (`features.hooks = true`). If they are off, the Stop gate cannot run. See `docs/STANDARDS.md` for its contract. A missing tool or a test with an error gives a gate error. The gate does not skip.

## Decisions that need approval to change

These are in `docs/adr/`:

- One pinned release and an in-place upgrade (0001).

- The installer has only `--fresh` and `--upgrade` (0002).

- `bin/muninn` finds its interpreter at run time (0003).

- Logs hold no transcript text (0004).

- There is no hash-chained call log (0005).

- A gate enforces the standards (0006).

- Recall for each prompt is off on a clean install (0007).

- `bin/muninn-install` picks the mode, and `--check` writes no files (0008).

- `bin/muninn-uninstall` keeps the data unless you tell it to remove the data (0009).

- Codex ChatGPT-handoff sessions are primary (0010).

- The schema migration goes in one direction. An upgrade first makes a snapshot of the store (0011).

- Content tombstones are HMAC tags with a key, and `tombstone.key` is a durable secret (0012).

- Cursor import is read-only during a full ingest, with no polling (0013).

- Pinned Pydantic validation and the new knowledge format (0014).

## Definition of done (Codex Stop hook and a manual fallback)

- The work is not done until `python3.13 -m tools.check --full` passes on the last tree. Run it after your last edit.

- A different agent can change a checked file after your run. Then your result is void. Run the gate again.

- Report a gate error as unfinished work with the lines that have errors. Do not report it as done. Write the result of the gate word for word in your last message.

- The trusted and approved Codex Stop hook blocks the end of work on an error. If hooks are off, not approved or not available in the host, run the full gate by hand. The same definition of done applies.

- A second Stop does not bypass an error. Find the cause or ask the owner for help before you try again.

- Your sandbox can stop writes in `.git`. Then you cannot commit or set hooks. Keep the changes uncommitted and say so. Let the approved operator commit them. The Git hook stays the enforcement point.

- Do not try to edit `.git/config` or the hooks.

- `git status` can say "must be run in a work tree". Then something changed `core.bare` to `true`. Stop and tell the owner.

## Verify a change

Run the narrowest tests first. Then run the full suite. Report the checks that you ran and the limits of the verification. A live launchd test and an installation on a different physical machine need an isolated manual check.
