# HANDOFF: pctx (updated 2026-10-01)

## Status

- pctx is the only memory system on this machine; it is installed and running
  from one pinned release under `~/.local/lib/provenance-context/`.
- Branch `feature/memsys-v2`. Hardening before a colleague installer is done:
  database size monitoring and `pctx compact`, `events_by_provider` and
  `skipped_files` in `stats`, structured `poller.log`, `--pretty`, an
  interpreter lookup in `bin/pctx`, an install record and install log, and
  `install.installer` with `--fresh` and `--upgrade` (one pinned release).
- Docs: `docs/QUICKSTART.md`, `docs/TROUBLESHOOTING.md`,
  `docs/ARCHITECTURE.md`, `AGENTS.md`, `README.md`.

## Upgrade this machine

    python3.13 -E -s -B -m install.installer --repo . \
        --sha "$(git rev-parse HEAD)" --upgrade

## Not done

- Retrieval and answer-quality checks are the next product work (see the
  memory file `project_memsys_pctx_rebuild.md`).
- `--fresh` has only been rehearsed in a temp HOME, never on a second machine.
- Item deferred by the owner: a hash-chained call log.

## Tests

    python3.13 -m unittest discover -s tests -t .
