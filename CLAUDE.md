# CLAUDE.md

Repository guidance for Claude Code.

`AGENTS.md` (layout and rules) and `README.md` (behaviour, privacy, hooks) are
authoritative; read them first. `docs/REFERENCE.md` is the full command, flag, env-var
and exit-code reference. `docs/adr/` records architecture decisions (one pinned release,
two installer modes, content-free logs, no hash-chained call log). Changes to these
decisions require approval.

## Commands

    python3.13 -m unittest discover -s tests -t .              # full suite
    python3.13 -m unittest tests.test_query                    # one module
    python3.13 -m unittest tests.test_query.AnswerEvidenceTests.test_open_and_neighbours_mark_answer_evidence  # one test
    python3.13 -m tools.check --full                           # the whole standards gate (needs .venv, see docs/STANDARDS.md)

Line length is 79 (black and ruff, configured in `pyproject.toml`). Run the CLI from
source with `bin/muninn ...` (or `python3.13 -m muninn`). Upgrade the installed machine
copy:

    bin/muninn-install --check   # plain-sentence preview, writes nothing
    bin/muninn-install           # fresh or upgrade, installs HEAD
    bin/muninn-uninstall --dry-run   # previews removal; actual uninstall is owner-only

## Architecture

- **Data flow:** provider transcripts (Claude Code JSONL, Codex sessions) are read-only
  inputs. `ingest_plan.py` decides what each file needs and `ingest_parse.py` parses the
  new lines; `classify.py` (with `claude_events.py`, `codex_events.py`,
  `tool_errors.py`) decides thread class (primary / subagent / guardian / fork replay)
  and event kind, `redaction.py` removes secrets, and events are stored in one SQLite
  file (`store.py` owns schema and the writer lock). `query/` (package:
  search/open/sessions, FTS5) and `knowledge*.py` (cited ledger) read it; `hook*.py`
  turns query results into the bounded SessionStart / UserPromptSubmit blocks;
  `scope.py` maps a cwd to a repository scope (worktrees fold into the main repo).
- **Single writer:** poller (`serve`), `ingest`, `erase`, `compact`, `rebuild`
  (`cli_rebuild.py` takes `writer_lock`), `know add` all take the exclusive
  `writer.lock` (`fcntl.flock` on macOS/Linux, `msvcrt.locking` on Windows); readers
  open read-only. Journal mode is DELETE. Exit code 3 means busy; 4 means store
  unavailable, a hot journal, or a missing or damaged `tombstone.key`
  (`tombstone_key.py`); 1 means `doctor` found an error-level problem.
- **Trust boundary:** all stored text is untrusted. Hook output is framed with
  `<muninn-memory ...>`; `classify.FLAG_MARKERS` flags any stored text containing that
  marker (or `<muninn-recall`, or the CLI notice sentence) so pasted blocks never come
  back as normal prompts. Entry headlines also reach the human in the hook's
  `systemMessage` (control characters replaced, never logged). Changing a marker or
  frame delimiter touches `hook_frame.py`, `event_model.py` (where the markers live) and
  the tests together.
- **Erase is tombstone-first:** `erase.py` writes tombstones (`tombstones.py`: line
  tombstones hold ids and hashes, content tombstones are keyed HMAC tags made with
  `tombstone.key`, never plain hashes of text; `erase_family.py` extends an erase to
  fork families) that `ingest_plan._tombstoned` checks before any line is read, so
  rescans can't resurrect content. Any new place that stores derived text must be
  covered by erase and by its residue scan.
- **Logging/observability:** `obs*.py` owns `calls.jsonl`, `status.json`, `poller.log`,
  `stats`, `doctor` (`obs_log`, `obs_status`, `obs_stats`, `obs`). Every log and report
  is allowlisted (ADR 0004); new fields go through the allowlist and never carry
  transcript text.
- **Deployment:** managed services (launchd, systemd user service or Task Scheduler) and
  provider hooks run the installed pinned release. macOS/Linux use
  `~/.local/lib/muninn/current/bin/muninn`; Windows uses the stable
  `muninn.cmd`/`muninn.ps1` launcher pair and `lib/selection.json`. A source change has
  no effect on the live system until `--upgrade` is run, and a successful upgrade
  deletes the previous release (no rollback to the old release; ADR 0001).
  `integrations/` and `launchd/` are templates with literal `@HOME@` that
  `install/steps_release.py` (`_unpack`) substitutes; `install/rollback.py` only undoes
  a failed or interrupted run, and also restores the pre-upgrade store copy
  (`install/snapshot.py`) if the new release had migrated the store.

## Engineering standards

`docs/STANDARDS.md` defines the enforced rules: files up to 400 non-blank lines,
functions up to 100, Google docstrings on everything, full annotations (pyright strict),
comments explaining constraints with no design/spec/work-unit labels, explicit top-level
imports, ruff and black clean. There is no `noqa`, no `# type: ignore` and no exemption
list. Claude Code's Stop hook (`.claude/settings.json`) and the Git pre-commit hook run
the full gate. `tests/test_standards.py` checks stdlib rules and enforcement
configuration; the installer enforces stdlib rules S1 to S8. Required checks must pass
before completion, commit or install. Changes must satisfy these rules; policy changes
require an ADR. These rules override any skill that says to document less (for example
"public docstrings only").

## Operational constraints

- `bin/muninn` runs `python3.13 -I -B` (isolated mode): no `PYTHONPATH`, no user site.
  Don't rely on either. It resolves its interpreter at run time (`$MUNINN_PYTHON`,
  installer link, then PATH; ADR 0003), so never hardcode a path.
- Hooks must always print compact JSON and exit 0 (fail open), even on store errors.
- Tests must use a temp `MUNINN_HOME`, and `MUNINN_ROOTS` to point at synthetic fixtures
  in `tests/fixtures`; installer tests use the fakes in `tests/test_installer.py` to
  prevent changes to the real HOME, native services or provider config.
- `.claude/skills/muninn-*` and `.codex/skills/muninn-*` mirror the CLI commands
  (`docs/SKILLS.md`). Keep them aligned with `docs/REFERENCE.md`; the
  SessionStart/UserPromptSubmit frame text stays minimal and does not name them.
