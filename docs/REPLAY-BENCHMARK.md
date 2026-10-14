# Measuring retrieval misses

`calls.jsonl` outcome flags are facts about a call (nothing returned, nothing in
scope), not proof that the answer was missing. True misses need labeled
questions. The replay tool runs your question set against a store, read-only.

```sh
python3.13 -m tools.replay_retrieval QUESTIONS.json [--home DIR] \
  [--all-projects | --no-all-projects] [--scope DIR] [--cwd DIR] \
  [--candidates 300] [--final 10]
```

`QUESTIONS.json` is your own file; the tool never echoes query text:

```json
{"questions": [{"id": "q1", "query": "...", "required": ["REF"], "control": false}]}
```

`--home` selects the data directory (default `$MUNINN_HOME`, else the standard
data home). `--all-projects` (default on) searches every project. `--cwd` is the
scope root and is used only with `--no-all-projects` and no `--scope`;
`--scope` keeps only events whose cwd is exactly that path. `--candidates`
(default 300, the real bm25 candidate pool) and `--final` (default 10, 1 to 30)
are the cut-offs. `--final` is an upper bound: the page byte budget can return
fewer hits, and those missing from them are not in final. A `--candidates` below
300 makes `rank` use a smaller pool than the real selector, so `in_final` can be
true while `rank` is null. The tool opens the store read-only, logs no calls and
writes nothing under `MUNINN_HOME`. Ranks use the same eligibility as
`muninn search` (primary threads, default kinds, unflagged), with no current
session excluded, so runs are reproducible. `in_final` comes from the real final
selector, including exact-repeat collapse and the per-session cap of 2: a
required event that is collapsed or capped counts as not in final.

Per question it reports, as separate numbers: the rank of each required ref in
the candidate list (or null), whether all required refs are within the
candidates, whether all are in the final results, and whether each ref resolves
with `hash_ok` (citation support). Refs are opened by re-reading the transcripts
under `MUNINN_ROOTS`; a missing transcript means `resolves` is false. A control
question has no required refs; it passes only with 0 bm25 candidates, and only
if the query has FTS terms. That is strict, because FTS queries are OR queries
and a control can match on one common word. A query with no terms gets
`"no_terms": true`: a control with no terms is not passed, and a gold question
with no terms is flagged for you to fix.

A required ref that ranks inside the candidates but is not in the final results
is usually removed by the per-session cap or exact-repeat collapse: on a real
store the cap removed over half of 300 candidates (see `session_capped` in a
search's `stages`). That gap between candidates and final is the number to watch.

Two practical notes from running it on a real store. Use fresh words in a
control query: words from any indexed session, including the session that runs
the replay, will match and fail the control. And pick required refs whose text
contains your query's terms; a bare approval such as a one-word "yes" prompt
cannot be found by a full-text query and reports `rank: null`.

Aggregates: candidate recall, complete required set at final, citation-resolves
rate, controls passed, `unresolved_refs` (required refs whose `resolves` is
false) and `no_terms_questions`. Output holds ids, ranks and counts only.

Exit codes: 0 whenever a report is printed (report-only: misses never change the
exit code), 2 for an invalid questions file (duplicate ids or duplicate refs
included), option or scope, 4 when the store cannot be read. Errors print a short
one-line message to stderr, never a traceback or question text.

Cursor has no UserPromptSubmit hook, so only `muninn search` outcomes apply to
Cursor content; `hook prompt` outcomes never cover it. Take each REF from the
`ref` field of `muninn search` output. The questions file must be your own data
and is never committed.

### Trying a different per-session cap

`--per-session N` (default 2, an integer of 1 or more) sets the per-session cap
used for the final list, so `in_final` reflects it; the candidate ranks are
unaffected. The cap counts hits across the whole candidate list, not per page,
and the hits it drops are gone for good: paging does not bring them back.

Each question reports `final_hits` (controls too), and each question that has
required refs also reports `distinct_sessions` (sessions in the final list),
`max_session_share` (the largest fraction of the final list that one session
supplies) and `final_bytes` (bytes of the returned hits as search reports
them). The summary adds the mean of each over those questions. Counts and
ratios only, as before.
