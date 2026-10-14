# Retrieval outcome log and replay benchmark: design

Status: draft for implementation review. Branch `feature/retrieval-outcome-log`.

## Intent

The owner's own agent-memory research (`~/Desktop/agent-memory-mechanisms.md`,
`claude:521e4a98-3afd-44be-bc4b-0c0814ed7a00:53.1`) gates any architecture
upgrade (graph, entity pool) on measured retrieval failures. Muninn cannot count
failures today. Per-prompt recall also ships off (ADR 0007) because relevance was
never validated. This change adds the measurement, with minimal risk.

Two kinds of "miss" exist and the design keeps them separate:

1. **Outcome facts** (runtime, content-free): a call returned nothing, only
   out-of-scope matches, no searchable terms, or everything fell below the term
   floor. These are facts, not proof of a miss.
2. **True misses** (offline, labeled): the answer existed but was not retrieved.
   Only a labeled question set can show this, so a replay tool measures it.

## Non-goals

No schema change, no new table, no new writes from reader or hook paths beyond
the existing `calls.jsonl` append, no query text or snippet stored anywhere,
no change to ranking or recall behaviour, no ADR change (ADR 0004 requires only
an allowlist entry plus a `tests/test_obs.py` test, and this adds none because
it reuses `stages` integer keys).

## Design

### A. Runtime outcome keys (extra integer keys inside `stages`)

`stages` already passes the allowlist as a map of `[a-z_]{1,30}` keys to ints
(`muninn/obs_log.py:48`). Add these 0/1 keys, absent when 0 is not meaningful:

| key | set when |
|---|---|
| `miss_zero` | searchable terms existed, `matches == 0`, no hits and no knowledge |
| `miss_outside` | `in_scope == 0`, `matches > 0`, no hits and no knowledge |
| `miss_noterms` | query had no searchable terms (search: `n_terms == 0`) |
| `miss_floor` | hook prompt only: `floor_dropped > 0`, no hits and no entries |

"No knowledge" matters: `search` returns knowledge separately (page 1) and a hook
block can exist with zero event hits, so a call that returned anything is not a
miss.

Search: compute in `muninn/cli_read.py` (where `out` is in hand). Hook: compute
in `muninn/hook_recall.py` `recall_block`, next to the existing trace writes.
`matches` / `in_scope` come from `tally()` over the whole query and are identical
on every page, so take **page 1's `stages` only** (never sum pages). `_pool`
currently discards `found`, so it returns page 1's stages too (a small return
change). No new SQL. The hook does **not** log `query_sha12` (it would add a
guessable-prompt hash for every prompt; the owner only wants counts).

Wrap the new hook code in a narrow `try/except` inside `recall_block`'s trace
writes so an error cannot reach `_build_text`'s `except Exception` (which would
emit "memory unavailable" to the model); keep that handler as the backstop.

A call that was skipped (`skipped` trace, `MUNINN_HOOK_DISABLE`, subagent,
`recall_off`) or errored is never a miss. Error and no-terms paths return before
`stages` exists, so "no stages" already means "no miss"; a pool break on `error`
must leave `matches` absent, not 0 (test it).

### B. Stats surface

`muninn stats` gains `retrieval_outcomes`, counts only, from the existing two-file
`calls.jsonl` window. Generalise `_hash_mismatches` (`muninn/obs_stats.py:76-89`)
into one pass; do not add a second scan. Shape:

    "retrieval_outcomes": {
      "window_lines": N,
      "search": {"calls": N, "zero": n, "outside": n, "noterms": n},
      "hook_prompt": {"calls": N, "zero": n, "outside": n, "floor": n}
    }

Calls without `stages` are counted in `calls` only. Torn lines are tolerated.
`search` counts lines with `cmd == "search"`; `hook_prompt` counts only the
UserPromptSubmit hook line (`cmd == "hook prompt"`, `cli_hook.py:95`).
No doctor check (YAGNI); stats is the observability surface.

### C. Offline replay benchmark: `tools/replay_retrieval.py`

Run as `python3.13 -m tools.replay_retrieval QUESTIONS.json [--home DIR]`.
Follows `tools/benchmark_cursor_precompact.py` (private helpers, `main(argv)`,
JSON to stdout). Input (user-owned file; outputs never echo query text):

    {"questions": [{"id": "q1", "query": "...", "required": ["REF", ...],
                    "control": false}]}

Reads a store **read-only**, never logs calls (set no-calllog), writes nothing
under `MUNINN_HOME`. The cut-offs are parameters, because the real values are
`CANDIDATES = 300` (`query/hits.py:13`), search page limit max 30, hook
`MAX_EVENTS = 3`: `--candidates` (default 300) and `--final` (default 10).
Per question it reports, as separate numbers: the rank of each required
original in the bm25 candidate list (or null), whether all required originals
are within the candidates, whether all are in the final results (search with
limit `--final`), and whether each required ref resolves with `hash_ok`
(citation support). Controls: report the result count; "pass" means 0 hits,
documented as strict (FTS queries are OR queries). Aggregates: candidate recall,
complete-required-set@final, citation-resolves rate, controls passed. Output
carries ids, ranks and counts only. A hook-equivalent @3 line is optional.

**Fidelity requirement.** Ranks must come from the same eligibility the real
search applies (primary threads, default kinds, unflagged, scope, current-session
exclusion); a whole-store rank would not be comparable to the owner's earlier
34-question experiments. `_eligibility` and the scope helpers are private, and
pyright strict forbids importing them from `tools/`, so extract one **public,
behaviour-neutral** function in `muninn/query/ranked.py` that returns the bm25
candidate rows for given args, and make `search` call it. Proof: the existing
query test suite passes unchanged plus one new test asserting `search` output is
identical before and after the extraction on a synthetic store.

### D. Documentation

README privacy line (:28-31), `docs/REFERENCE.md` (outcome paragraph beside
"Withheld is counted" :117-124, `muninn stats` fields :209-231, files table
:319-320), `docs/TROUBLESHOOTING.md` :42, `AGENTS.md` :27 (`tools/`), new
`docs/REPLAY-BENCHMARK.md` (sibling of the Cursor benchmark doc), and the
`muninn-stats` skill in both `.claude/skills` and `.codex/skills` (stats gains a
field). Honest framing everywhere: outcome counts are facts, not proof of a miss.

## Risk controls

- Kill switch: existing `MUNINN_NO_CALLLOG=1`.
- Line cap: at most 4 extra short keys; the 1 KiB cap still applies and is tested.
- Fail open: new hook code is wrapped; a test forces an exception and asserts `{}`
  or the normal block.
- No new lock use, no store write, no schema change; replay opens read-only.
- Privacy: only ints and the existing 12-hex query hash; canary tests prove no text.

## Proof, observability, traceability

- **Proof:** unit tests per outcome (zero, outside, noterms, floor, skipped is not
  a miss), allowlist canary test, hook e2e extension, stats aggregation test with
  torn lines, replay test on a synthetic store with known gold ranks and a
  control, `test_obs` line-cap test with the new keys, and `tools.check --full`.
- **Observability:** `muninn stats` `retrieval_outcomes`; `logged` flag unchanged.
- **Traceability:** the PR body carries a table requirement -> file -> test ->
  doc, one commit per work stream, and the demo output of the replay tool on the
  synthetic fixture.

## Work split (each stream in its own git worktree, branched from this branch)

| stream | branch | files | depends on |
|---|---|---|---|
| A runtime keys | `feature/outcome-keys` | `cli_read.py`, `hook_recall.py`, tests | contract above only |
| B stats | `feature/outcome-stats` | `obs_stats.py`, stats tests, skill mirrors | key names only |
| C replay tool | `feature/outcome-replay` | `tools/replay_retrieval.py`, `muninn/query/ranked.py` (public candidate accessor), tests | query internals |
| D docs | `feature/outcome-docs` | docs listed in D | contract only |

Merge A-D into `feature/retrieval-outcome-log`, run the full gate, independent
review, then open one PR. Never merge it; never edit `main`; never run the
installer (owner only).

## Acceptance criteria

1. Each of the four outcomes appears in `calls.jsonl` exactly as specified and
   never for skipped or errored calls.
2. No line exceeds 1 KiB; no query text reaches any file (canary tests).
3. `muninn stats` reports `retrieval_outcomes` from synthetic logs correctly.
4. Replay tool reports the five numbers separately on a synthetic store and
   fails closed (non-zero exit, no output of text) on a malformed input file.
5. Search and hook output is byte-identical to before for the same inputs
   (apart from `calls.jsonl` content).
6. All docs above updated; `python3.13 -m tools.check --full` passes.
