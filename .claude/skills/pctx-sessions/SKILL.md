---
name: pctx-sessions
description: List and browse past sessions with `pctx sessions` and `pctx session ROOT`. Use when the user asks what sessions exist, what happened last week/yesterday, to see one session in order, or when you need a session id to scope a search.
---

# pctx sessions / session

Browse instead of search, when you know roughly when but not what.

    pctx sessions --since 2026-09-30 --limit 20      # newest first, this repo
    pctx session ROOT                                # one session, in order

- `ROOT` is the `session` value from `sessions[]`.
- `sessions` flags: `--all-projects`, `--since`, `--limit`. Each row has provider, event and thread counts, first/last time and a `preview`.
- `session` flags: `--from N` to continue with `next_from` from the previous page. Events carry `ref`, `kind`, `role`, `ts`, `preview`, `answer_citable`.
- Previews are navigation only: take a `ref` and run `pctx open REF` before citing.
- Feed a session id to `pctx search --session ID` to search inside it.
