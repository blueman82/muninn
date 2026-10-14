# Measuring retrieval misses

`calls.jsonl` outcome flags are facts about a call (nothing returned, nothing in
scope), not proof that the answer was missing. True misses need labeled
questions. The replay tool runs your question set against a store, read-only.

```sh
python3.13 -m tools.replay_retrieval QUESTIONS.json \
  [--home DIR] [--candidates 300] [--final 10]
```

`QUESTIONS.json` is your own file; the tool never echoes query text:

```json
{"questions": [{"id": "q1", "query": "...", "required": ["REF"], "control": false}]}
```

`--home` selects the data directory (default `MUNINN_HOME`). `--candidates`
(default 300, the real bm25 candidate pool) and `--final` (default 10, the search
page limit; the hook shows at most 3) are the cut-offs. The tool opens the store
read-only, logs no calls and writes nothing under `MUNINN_HOME`. Ranks use the
same eligibility as `muninn search` (primary threads, default kinds, unflagged,
scope, current session excluded).

Per question it reports, as separate numbers: the rank of each required ref in
the candidate list (or null), whether all required refs are within the
candidates, whether all are in the final results, and whether each ref resolves
with `hash_ok` (citation support). A control question has no required refs;
passing means 0 hits. That is strict, because FTS queries are OR queries and a
control can match on one common word.

Aggregates: candidate recall, complete required set at final, citation-resolves
rate, controls passed. Output holds ids, ranks and counts only. A malformed input
file exits non-zero with no text output.
