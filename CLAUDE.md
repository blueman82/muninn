# CLAUDE.md

Repository guidance for Claude Code.

`AGENTS.md` (layout and rules) and `README.md` (behavior, privacy, hooks) are authoritative. Read them first.

`docs/REFERENCE.md` lists all commands, flags, env-vars and exit codes.

`docs/adr/` records architecture decisions. The decisions are one pinned release, two installer modes, content-free logs and no hash-chained call log. A change to these decisions needs approval.

## Commands

    python3.13 -m unittest discover -s tests -t .              # all tests
    python3.13 -m unittest tests.test_query                    # one module
    python3.13 -m unittest tests.test_query.AnswerEvidenceTests.test_open_and_neighbours_mark_answer_evidence  # one test
    python3.13 -m tools.check --full                           # all standards checks (needs .venv, see docs/STANDARDS.md)

Line length is 79 (black and ruff, set in `pyproject.toml`). Run the CLI from source with `bin/muninn ...` or `python3.13 -m muninn`. These commands upgrade the installed machine copy:

    bin/muninn-install --check       # plain-sentence preview, writes no files
    bin/muninn-install               # clean install or upgrade, installs HEAD
    bin/muninn-uninstall --dry-run   # previews the removal, only the owner can uninstall

## Architecture

- **Data flow:** provider transcripts (Claude Code JSONL, Codex sessions) are read-only inputs.

- `ingest_plan.py` decides what each file needs. `ingest_parse.py` parses the new lines.

- `classify.py` decides the thread class (primary, subagent, guardian or fork replay) and the type of each item. It uses `claude_events.py`, `codex_events.py` and `tool_errors.py`.

- `redaction.py` removes secrets. One SQLite file holds the data. `store.py` owns the schema and the writer lock.

- `query/` (a package for search, open and sessions, with FTS5) and `knowledge*.py` (the cited ledger) read the data.

- `hook*.py` turns query results into the bounded SessionStart and UserPromptSubmit blocks. `scope.py` maps a cwd to a repository scope. Worktrees fold into the primary repository.

- **Single writer:** these commands take the exclusive `writer.lock`. They are the poller (`serve`), `ingest`, `erase`, `compact` and `rebuild`. Also `learn request`, `know propose` and `know retract`.

- `cli_rebuild.py` takes `writer_lock` for `rebuild`.

- The lock is `fcntl.flock` on macOS and Linux. It is `msvcrt.locking` on Windows. Readers open the store read-only. Journal mode is `DELETE`.

- Exit code 3 means busy. Exit code 4 means the store is not available, or a hot journal. It also means a missing or damaged `tombstone.key` (`tombstone_key.py`).

- Exit code 1 means `doctor` found an error-level problem.

- **Trust boundary:** you must not trust text in the store. Hook output has the frame `<muninn-memory ...>`.

- `classify.FLAG_MARKERS` flags all text in the store that has that marker, `<muninn-recall`, or the CLI notice sentence. A block that the user copies in does not come back as a usual prompt.

- Entry headlines also go to the human in the `systemMessage` of the hook. The code replaces control characters. The code does not write headlines to a log.

- A change to a marker or a frame delimiter touches `hook_frame.py`, `event_model.py` and the tests together. The markers are in `event_model.py`.

- **Erase writes tombstones first:** `erase.py` writes tombstones (`tombstones.py`). Line tombstones hold ids and hashes. Content tombstones are HMAC tags that use `tombstone.key` as the key. They are not plain hashes of text.

- `erase_family.py` extends an erase to fork families. `ingest_plan._tombstoned` checks tombstones before it reads a line, so a rescan cannot bring back content.

- Each new position that keeps derived text must be in the erase and in its scan for remaining text.

- **Logging and observability:** `obs*.py` owns `calls.jsonl`, `status.json`, `poller.log`, `stats` and `doctor`.

- The modules are `obs_log`, `obs_status`, `obs_stats` and `obs`.

- An allowlist controls all logs and reports (ADR 0004). New fields go through the allowlist. They do not contain transcript text.

- **Deployment:** managed services (launchd, a systemd user service or Task Scheduler) and provider hooks run the installed pinned release. macOS and Linux use `~/.local/lib/muninn/current/bin/muninn`.

- Windows uses the stable launcher pair `muninn.cmd` and `muninn.ps1`, and `lib/selection.json`.

- A source change has no effect on the live system until you run `--upgrade`. An upgrade that completes erases the previous release. You cannot go back to that release (ADR 0001).

- `integrations/` and `launchd/` hold templates with the literal `@HOME@`. `install/steps_release.py` (`_unpack`) replaces it.

- `install/rollback.py` only undoes a run that stops with an error or an interruption. It also repairs the store copy from before the upgrade (`install/snapshot.py`) if the new release changed the store.

## Engineering standards

`docs/STANDARDS.md` defines the enforced rules. Files have up to 400 non-blank lines. Functions have up to 100 lines. Google docstrings are on all items. Types are full (pyright strict).

Comments explain constraints and have no design, spec or work-unit labels. Imports are explicit and at the top. Ruff and black give no errors. There is no `noqa`, no `# type: ignore` and no exemption list.

Claude Code has no Stop hook. Its PostToolUse hook (`.claude/settings.json`) runs the fast check after each edit. The Git pre-commit hook runs all checks. Run `python3.13 -m tools.check --full` before you end a task.

`tests/test_standards.py` checks the stdlib rules and the enforcement settings. The installer enforces stdlib rules S1 to S8. The necessary checks must go before you end a task, make a commit or install.

Changes must satisfy these rules. A change to the policy needs an ADR. These rules override a skill that says to write fewer docs, for example "public docstrings only".

## Operational constraints

- `bin/muninn` runs `python3.13 -I -B` (isolated mode). It has no `PYTHONPATH` and no user site. Do not use them.

- `bin/muninn` finds its interpreter at run time (`$MUNINN_PYTHON`, the installer link, then PATH, ADR 0003). Do not write a path in the code.

- Hooks must print compact JSON and exit 0 (fail open) at all times. This includes store errors.

- Tests must use a temp `MUNINN_HOME`. They must use `MUNINN_ROOTS` to point at synthetic fixtures in `tests/fixtures`.

- Installer tests use the fakes in `tests/test_installer.py`. The fakes stop changes to the HOME of the user, native services and provider config.

- `.claude/skills/muninn-*` and `.codex/skills/muninn-*` mirror the CLI commands. Keep them equal to `docs/REFERENCE.md`.

- The frame text of SessionStart and UserPromptSubmit stays minimal and does not name the skills.
