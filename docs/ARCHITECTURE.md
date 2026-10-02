# Architecture

```mermaid
flowchart LR
  subgraph Providers["Provider transcripts (read only)"]
    CC["Claude Code<br/>~/.claude/projects"]
    CX["Codex<br/>sessions + archived"]
  end
  P["poller<br/>muninn serve (launchd, 60 s)"]
  DB[("muninn.sqlite<br/>events, knowledge,<br/>tombstones, FTS5")]
  CLI["muninn CLI<br/>search, open, know,<br/>stats, doctor, compact"]
  H["hooks<br/>SessionStart,<br/>UserPromptSubmit"]
  A["Claude Code / Codex<br/>session"]
  L["status.json, poller.log,<br/>calls.jsonl"]
  CC --> P
  CX --> P
  P -->|"ingest: classify, redact"| DB
  P --> L
  DB --> CLI
  DB --> H
  H -->|"bounded, framed,<br/>untrusted-data"| A
  A -->|"runs"| CLI
  CLI --> L
```

- **One writer at a time.** The poller, `ingest`, `erase`, `compact` and
  `know add` take the flock on `writer.lock`; readers open the database
  read-only. The journal mode is DELETE, so a crash leaves a journal that the
  next writer rolls back.
- **Pinned release.** launchd and the hooks run
  `~/.local/lib/muninn/current/bin/muninn`; `bin/muninn` picks the
  interpreter from `$MUNINN_PYTHON`, then the installer's `python` link, then
  Python 3.13+ on PATH.
- **Installer.** `install/installer.py` has two modes (`--fresh`,
  `--upgrade`) over one step pipeline: record, pin, build or restart, merge
  provider config, verify, prune. `install/rollback.py`
  reverses it from the recorded values.
- **Trust.** Everything stored is untrusted historical data. Secrets are
  redacted at ingest and on output; automatic injection is framed so pasted
  copies are flagged on ingest. See the README for the full list.
