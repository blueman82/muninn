# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` (layout and rules) and `README.md` (behaviour, privacy, hooks) are authoritative; read them first. `docs/REFERENCE.md` is the full command, flag, env-var and exit-code reference. `docs/adr/` records owner decisions (one pinned release, two installer modes, content-free logs, no hash-chained call log); don't reverse one without asking.

## Commands

    python3.13 -m unittest discover -s tests -t .              # full suite
    python3.13 -m unittest tests.test_query                    # one module
    python3.13 -m unittest tests.test_query.AnswerEvidenceTests.test_open_and_neighbours_mark_answer_evidence  # one test
    python3.13 -m tools.check --full                           # the whole standards gate (needs .venv, see docs/STANDARDS.md)

Line length is 79 (black and ruff, configured in `pyproject.toml`). Run the CLI from source with `bin/pctx ...` (or `python3.13 -m pctx`). Upgrade the installed machine copy:

    python3.13 -E -s -B -m install.installer --repo . --sha "$(git rev-parse HEAD)" --upgrade

## Architecture (the parts that span files)

- **Data flow:** provider transcripts (Claude Code JSONL, Codex sessions) are read-only inputs. `ingest.py` parses them, `classify.py` decides thread class (primary / subagent / guardian / fork replay) and event kind, secrets are redacted, and rows land in one SQLite file (`store.py` owns schema and the writer lock). `query.py` (search/open/sessions, FTS5) and `knowledge.py` (cited ledger) read it; `hook.py` turns query results into the bounded SessionStart / UserPromptSubmit blocks; `scope.py` maps a cwd to a repository scope (worktrees fold into the main repo).
- **Single writer:** poller (`serve`), `ingest`, `erase`, `compact`, `know add` all take the flock on `writer.lock`; readers open read-only. Journal mode is DELETE. Exit code 3 means busy; 4 means store unavailable or a hot journal; 1 means `doctor` found an error-level problem.
- **Trust boundary:** all stored text is untrusted. Hook output is framed with `<pctx-memory ...>`; `classify.FLAG_MARKERS` flags any stored text containing that marker (or `<pctx-recall`, or the CLI notice sentence) so pasted blocks never come back as normal prompts. Changing a marker or frame delimiter touches `hook.py`, `classify.py` and the tests together.
- **Erase is tombstone-first:** `erase.py` writes tombstones (ids and hashes only) that `ingest._tombstoned` checks before any line is read, so rescans can't resurrect content. Any new place that stores derived text must be covered by erase and by its residue scan.
- **Logging/observability:** `obs.py` owns `calls.jsonl`, `status.json`, `poller.log`, `stats`, `doctor`. Every log and report is allowlisted (ADR 0004); new fields go through the allowlist and never carry transcript text.
- **Deployment:** launchd and the hooks run the pinned release at `~/.local/lib/provenance-context/current/bin/pctx`, not this checkout. A source change has no effect on the live system until `--upgrade` is run, and a successful upgrade deletes the previous release (no rollback; ADR 0001). `integrations/` and `launchd/` are templates with literal `@HOME@` that `install/installer.py` substitutes; `install/rollback.py` only undoes an interrupted run.

## Standards are enforced, not advisory

`docs/STANDARDS.md` is the rulebook: files up to 400 lines, functions up to 100, Google docstrings on everything, full annotations (pyright strict), why-comments with no design/spec/work-unit labels, explicit top-level imports, ruff and black clean. There is no `noqa`, no `# type: ignore` and no exemption list. Claude Code's Stop hook (`.claude/settings.json`), the git pre-commit hook, `tests/test_standards.py` and the installer all run the gate, so a change that breaks a rule cannot be finished, committed or installed. Fix the code, not the rule. These rules override any skill that says to document less (for example "public docstrings only").

## Gotchas

- `bin/pctx` runs `python3.13 -I -B` (isolated mode): no `PYTHONPATH`, no user site. Don't rely on either. It resolves its interpreter at run time (`$PCTX_PYTHON`, installer link, then PATH; ADR 0003), so never hardcode a path.
- Hooks must always print compact JSON and exit 0 (fail open), even on store errors.
- Tests must use a temp `PCTX_HOME`, and `PCTX_ROOTS` to point at synthetic fixtures in `tests/fixtures`; installer tests use the fakes in `tests/test_installer.py` so nothing touches the real HOME, launchd or provider config.
- `HANDOFF.md` holds current status and deferred work; update it when work spans sessions.
