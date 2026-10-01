# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` (layout and rules) and `README.md` (behaviour, privacy, hooks) are authoritative; read them first. This file only adds what they don't.

## Commands

    python3.13 -m unittest discover -s tests -t .              # full suite
    python3.13 -m unittest tests.test_query                    # one module
    python3.13 -m unittest tests.test_query.AnswerEvidenceTests.test_open_and_neighbours_mark_answer_evidence  # one test
    ruff check . && black --check . && isort --check-only .    # needs a venv with the tools

Line length is 79 (black, ruff, isort all configured in `pyproject.toml`). Run the CLI from source with `bin/pctx ...` (or `python3.13 -m pctx`). Upgrade the installed machine copy:

    python3.13 -E -s -B -m install.installer --repo . --sha "$(git rev-parse HEAD)" --upgrade

## Architecture (the parts that span files)

- **Data flow:** provider transcripts (Claude Code JSONL, Codex sessions) are read-only inputs. `ingest.py` parses them, `classify.py` decides thread class (primary / subagent / guardian / fork replay) and event kind, secrets are redacted, and rows land in one SQLite file (`store.py` owns schema and the writer lock). `query.py` (search/open/sessions, FTS5) and `knowledge.py` (cited ledger) read it; `hook.py` turns query results into the bounded SessionStart / UserPromptSubmit blocks; `scope.py` maps a cwd to a repository scope (worktrees fold into the main repo).
- **Single writer:** poller (`serve`), `ingest`, `erase`, `compact`, `know add` all take the flock on `writer.lock`; readers open read-only. Journal mode is DELETE. Exit code 3 means busy.
- **Trust boundary:** all stored text is untrusted. Hook output is framed with `<pctx-memory ...>`; ingest flags any stored text containing that marker so pasted blocks never come back as normal prompts. Changing a marker or frame delimiter touches `hook.py`, `ingest.py`/`classify.py` and the tests together.
- **Erase is tombstone-first:** `erase.py` writes tombstones (ids and hashes only) that ingest checks before parsing a line, so rescans can't resurrect content. Any new place that stores derived text must be covered by erase and by its residue scan.
- **Logging/observability:** `obs.py` owns `calls.jsonl`, `status.json`, `poller.log`, `stats`, `doctor`. New log fields must pass its allowlist; never log transcript text.
- **Deployment:** launchd and the hooks run the pinned release at `~/.local/lib/provenance-context/current/bin/pctx`, not this checkout. A source change has no effect on the live system until `--upgrade` is run. `integrations/` and `launchd/` are templates with literal `@HOME@` that `install/installer.py` substitutes; `install/rollback.py` reverses the recorded config edits.

## Gotchas

- `bin/pctx` runs `python3.13 -I -B` (isolated mode): no `PYTHONPATH`, no user site. Don't rely on either.
- Hooks must always print compact JSON and exit 0 (fail open), even on store errors.
- Tests must use a temp `PCTX_HOME`, and `PCTX_ROOTS` to point at synthetic fixtures in `tests/fixtures`; installer tests use the fakes in `tests/test_installer.py` so nothing touches the real HOME, launchd or provider config.
- `HANDOFF.md` holds current status and deferred work; update it when work spans sessions.
