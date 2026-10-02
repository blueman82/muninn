# HANDOFF: pctx (updated 2026-10-02)

## Status

- pctx is the only memory system on this machine; it is installed and running
  from one pinned release under `~/.local/lib/provenance-context/`.
- Branch `main` is the only branch. Hardening before a colleague installer is
  done: database size monitoring and `pctx compact`, `events_by_provider` and
  `skipped_files` in `stats`, structured `poller.log`, `--pretty`, an
  interpreter lookup in `bin/pctx`, an install record and install log, and
  `install.installer` with `--fresh` and `--upgrade` (one pinned release).
- Engineering standards are enforced by a gate (`docs/STANDARDS.md`, ADR 0006):
  every Python file at most 400 non-blank lines, every function at most 100,
  Google docstrings, full annotations (pyright strict for `pctx/`, `install/`,
  `tools/`), why-comments with no design/spec labels, ruff and black clean.
  The code was split into small modules (`cli_*`, `ingest_*`, `knowledge_*`,
  `erase_*`, `hook_*`, `obs_*`, `query/`, `install/*`) with no behaviour change:
  the original tests pass and read-only commands match the previous release.
- Docs: `docs/REFERENCE.md`, `STANDARDS.md`, `QUICKSTART.md`,
  `TROUBLESHOOTING.md`, `ARCHITECTURE.md`, `adr/`, `AGENTS.md`, `CLAUDE.md`.

## One-time setup per clone

    git config core.hooksPath .githooks
    python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
    cp tools/claude-settings.json .claude/settings.json

The first two are checked by the gate and its tests. The third makes Claude
Code run the gate after every edit and refuse to stop while it fails; it needs
the owner because the model is not allowed to edit its own hook settings.

## Upgrade this machine

    python3.13 -E -s -B -m install.installer --repo . \
        --sha "$(git rev-parse HEAD)" --upgrade

The installer refuses a commit that breaks the stdlib standards rules.

## Not done

- Retrieval and answer-quality checks are the next product work (see the
  memory file `project_memsys_pctx_rebuild.md`).
- `--fresh` now creates `recall.off`, so per-prompt recall starts off
  (ADR 0007; merged to main but not installed until the next `--upgrade`,
  and an upgrade does not change an existing machine). To restore the old default, stop creating it in
  `install/steps_release.py: ingest_fresh` once the retrieval re-check passes.
- `--fresh` has only been rehearsed in a temp HOME, never on a second machine.
- Deferred by the owner: a hash-chained call log.
- `.claude/skills/pctx-*` (11 skills, `docs/SKILLS.md`) are committed. Not
  done: the installer does not ship them to other machines, there is no Codex
  equivalent, and nobody has checked in a fresh session that they trigger.
  Decision: the hook frame text stays unchanged and does not name them.
- `query.search` takes its options as `**args` with a hand-written keyword
  check rather than a real keyword-only signature (the signature would exceed
  the argument-count limit); the TypeError text differs slightly from Python's.

## Tests

    python3.13 -m unittest discover -s tests -t .
    python3.13 -m tools.check --full       # the whole gate
