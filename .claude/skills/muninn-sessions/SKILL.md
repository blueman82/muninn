---
name: muninn-sessions
description: List and browse past sessions with `muninn sessions` and `muninn session ROOT`. Use when the user asks what sessions exist, what happened last week/yesterday, to see one session in order, or when you need a session id to scope a search.
---

# muninn sessions / session

Browse instead of search, when you know roughly when but not what.

    muninn sessions --since 2026-09-30 --limit 20      # newest first, this repo
    muninn session ROOT                                # one session, in order

- `ROOT` is the `session` value from `sessions[]`.
- `sessions` flags: `--all-projects`, `--since`, `--limit`. Each row has provider, event and thread counts, first/last time and a `preview`.
- `session` flags: `--from N` to continue with `next_from` from the previous page. Events carry `ref`, `kind`, `role`, `ts`, `preview`, `answer_citable`.
- Previews are navigation only: take a `ref` and run `muninn open REF` before citing.
- Feed a session id to `muninn search --session ID` to search inside it.
