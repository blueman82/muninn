# HANDOFF — pctx memory + knowledge rebuild (paused 2026-09-30)

Full context: memory file `~/.claude/projects/-Users-garyharr-Github-provenance-context/memory/project_memsys_pctx_rebuild.md`
and the loop state dir `~/.coderails/agentic-loop/-Users-garyharr-Github-provenance-context-.git/1a23fdfe-87cc-4053-869b-3145ef08c81c/`
(`progress.json`, `spec.md`, `plan.md`, `design/`, `audit/`, `review/`, `reports/`, `trial/`). No private transcript
text is stored in this repository.

## Objective and status

- Objective: replace the old provenance-context daemon/injection with `pctx` (cross-provider memory + cited
  knowledge for Claude Code and Codex), prove it on the frozen matched trial, then cut the live machine over.
- Status: product BUILT on this branch (`feature/memsys-v2`); trial frozen at Stage B but NOT run; live cutover NOT
  done; the old tree `/Users/garyharr/Github/provenance-context-build` is untouched and still the live service + rollback.

## Decisions and assumptions

- Architecture B′ "pctx": stdlib CLI + KeepAlive poller, one SQLite file (DELETE journal), primary-thread default
  recall, pull tools, quote-verified knowledge ledger, tombstone erasure, SessionStart block + bounded per-prompt recall.
- Owner decisions recorded verbatim in `progress.json` (`authorising_prompt_raw`): build the prompt hook, full local
  cutover authorised, email-only isolation waiver, run the trial then cut over, pause for a new session.
- Rechecked round-four baseline: strict 20/34 (22 report rule; 23–24 protocol-as-written).

## Files changed (this branch vs b896cab)

- New package `pctx/` (store, scope, classify, ingest, query, knowledge, erase, hook, cli, obs), `bin/pctx`,
  `install/` (cutover, rollback, configedit), `integrations/` (Claude settings fragment, Codex plugin 0.2.0),
  `launchd/com.provenance-context.plist`, `trial_harness/`, `tests/`, `README.md`; retired legacy `scripts/`,
  `hooks/`, `claude-code/` and their tests (clean break).

## Tests and checks

- At `b9f7774`: `pytest` 676 passed + 638 subtests; ruff, black, isort clean (dev venv `/Users/garyharr/Github/memsys-wt/.venv`).
- Real-data ingest over the frozen transcript mirror: 2,360 files, 84,897 events, 0 failures; 53/53 required
  round-four originals reachable. Trial Stage B verifies CLEAN (`trial/FROZEN-B.sha256`).
- Not yet run: live cutover, in-session hooks in real Claude/Codex sessions, the trial itself.

## Remaining work, risks, blockers

1. Finish the trial driver (`memsys-v2/trial-run`, slice 1 at `3a383ee`; slices 2–6 in `trial/reports/T-RUN-driver-report.md`).
2. Canaries → dry run (4 units) → 114 reader units (≤4 concurrent) → blind double grading → report.
3. Cutover per `reports/WU11-report.md` §4 unless the trial shows NEW worse or a safety finding; then post-checks.
4. Post-verification: proofs, independent eval grading, erase trial sessions from the live DB, merge
   `memsys-v2/deliverable` (held until the trial report).
- Blocker for formal loop completion: coderails `hooks/scripts/lib/graph_evidence.py` `records()` must split on
  `"\n"` (not `splitlines()`).
- Owner actions: rotate the Confluence PAT printed into two local subagent transcripts; decide legacy-data deletion
  after the 7-day window.
