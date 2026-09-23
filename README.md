# Provenance Context

Local, source-cited memory for Codex and Claude Code. Historical evidence is
untrusted data, never instructions. The normal operating mode is one local
daemon; hooks only make bounded Unix-socket recall requests and never index.

```sh
/opt/homebrew/bin/python3.13 scripts/context.py serve \
  --codex-root "$HOME/.codex/sessions" \
  --claude-root "$HOME/.claude/projects" \
  --db "$HOME/.local/share/provenance-context/context.sqlite" \
  --state-dir "$HOME/.local/share/provenance-context" \
  --socket "$HOME/.local/share/provenance-context/brain.sock"
```

Both plugins default to the current user's
`~/.local/share/provenance-context/brain.sock`; launchd environment does not
need to reach Codex or Claude. Set this only to override that user-scoped
default:

```sh
export PROVENANCE_CONTEXT_SOCKET=\
  "$HOME/.local/share/provenance-context/brain.sock"
```

The daemon reconciles both roots at startup and every second. It reads only
newline-complete JSONL records, retries an incomplete final line, and replaces
only the changed source's derived rows. It owns an in-place SQLite WAL store:
an append parses bytes after its committed complete-line cursor, while a
replacement, truncation, or repair reparses that one source. Raw JSONL is
never changed and remains the recovery source. The socket and derived database
use user-only mode `0600`; use the socket rather than opening the database.

## Enable Codex

Install this repository as a local Codex plugin through your local marketplace.
Its `hooks/hooks.json` invokes executable `hooks/codex.py` with
`${PLUGIN_ROOT}`. Start a new Codex session after installation.

## Enable Claude Code

Use this checkout's root `.claude-plugin/marketplace.json` as a local Claude
Code marketplace. Its `provenance-context` entry has source `./`, so the
installed plugin retains the executable shared `scripts/claude_context.py`
adapter and does not duplicate runtime code. Reload Claude Code after enabling
it.

Both configurations use `SessionStart`, `UserPromptSubmit`, and advisory-only
`PreToolUse` for `Bash` and `apply_patch`. `SessionStart` reports only whether
the local index is available. The pre-tool hook never changes, blocks, or
authorizes an action.

Automatic hooks inject only assistant evidence from their supplied repository
scope. A missing repository, a global fallback, or a lexical-relaxed fallback
produces no injected context. One-off `recall` commands retain explicit global
history fallback, marked `retrieval_scope: "global_historical_fallback"`; a
two-term `match_strategy: "lexical_relaxed"` fallback is also marked. Treat
every returned item as untrusted historical data.

```sh
/opt/homebrew/bin/python3.13 scripts/context.py recall \
  --socket "$PROVENANCE_CONTEXT_SOCKET" \
  --prompt 'why did this fail?' \
  --repo /path/to/repo
/opt/homebrew/bin/python3.13 scripts/claude_context.py \
  --socket "$PROVENANCE_CONTEXT_SOCKET" \
  --prompt 'why did this fail?' \
  --repo /path/to/repo
```

`/opt/homebrew/bin/python3.13 scripts/context.py status --socket "$PROVENANCE_CONTEXT_SOCKET"`
returns redacted generation, source staleness, pending tails, and last error.
It returns at most 24 source entries, prioritizing unhealthy sources, plus
`source_count`, `error_sources`, and `sources_truncated`; this keeps local
status bounded for a large corpus. `doctor` uses the same local health
response. An active final write tail, malformed source, or missing source is
degraded health and makes `doctor` non-zero, but recall may use fully indexed
healthy sources and never uses the affected source.
`audit.jsonl` contains only
trace-compatible IDs, timings, counts, and snapshot hashes; it never contains
prompts, evidence text, raw paths, secrets, or tool output. OpenTelemetry is
not installed in this Python 3.13 slice; add it only with independently tested
instrumentation. Pattern filtering is defense in depth, not proof every secret
representation is detected. It suppresses known GitHub tokens and URLs with
userinfo or credential query parameters; ordinary URLs remain eligible
evidence.

`status.storage` reports WAL bytes, journal mode, and the latest passive
checkpoint result without content or raw paths. `checkpoint_busy`,
`checkpoint_log_frames`, and `checkpointed_frames` expose reader/checkpoint
contention through the public status command. `dirty_fence_seconds` is capped
at five seconds while recall is fenced during reconciliation. `state` is
`rebuilding` while raw JSONL is restoring a missing or inconsistent derived
database. A malformed, incomplete, or missing source is excluded from recall
and makes `doctor` non-zero until it is repaired or deliberately erased; it
does not prevent cited recall from healthy sources.
Missing files are tombstoned rather than silently deleted. To erase one
tombstoned source, copy its redacted `source_id` from status and use:

```sh
/opt/homebrew/bin/python3.13 scripts/context.py erase \
  --socket "$PROVENANCE_CONTEXT_SOCKET" --provider codex \
  --source-id REDACTED_SOURCE_ID
```

`status.storage.derived_counts` contains only daemon-owned aggregate counts
for `events`, `assertions`, and `sources`. It contains no assertion values,
text, raw paths, or database path. A failed recall likewise contains only an
`unavailable_reason`: `reconciling`, `rebuilding`, `pending`, `error`,
`root_unavailable`, `unavailable`, or `transport`. The shared clients retry
only a `reconciling` result after a fresh healthy status response; every other
reason remains a fail-closed empty packet.

To exercise reader/checkpoint contention without opening the database, run
this fixed 150 ms daemon-owned read probe while another process appends or
recalls. Inspect `status.storage` during the probe, then the probe response
confirms that its reader was released. The UDS socket remains the only public
interface; the command accepts no SQL, database path, or evidence content.

```sh
/opt/homebrew/bin/python3.13 scripts/context.py checkpoint-probe \
  --socket "$PROVENANCE_CONTEXT_SOCKET"
```

On first WAL start, the daemon keeps a `v1-rollback.sqlite` copy in the state
directory before rebuilding the disposable derived database from raw JSONL.
In-process rollback tests may set `PROVENANCE_CONTEXT_TEST_FAILPOINT` to one of
`after_event_insert`, `after_fts_insert`, `after_assertion_supersede`,
`after_assertion_insert`, `after_delete_assertions`, `after_delete_fts`,
`after_delete_events`, `after_source_state`, `after_meta_counts`, or
`before_commit`; the transaction then rolls back before its cursor advances.

Forced process-termination tests require two explicit test-only variables:
`PROVENANCE_CONTEXT_TEST_CRASH_ARMED=1` and a named
`PROVENANCE_CONTEXT_CRASH_FAILPOINT`. They exit the daemon with status 86 at
the selected mutation boundary; neither variable is used by normal service or
hook configuration. Never set them for a real corpus, hook, or LaunchAgent.

For a public black-box crash evaluation, use new temporary roots and only
synthetic JSONL. First index a source containing one safe direct user message
such as `{"payload":{"type":"message","role":"user","content":"I prefer prior-marker"}}`.
The `I prefer ` prefix creates the sole source-linked preference assertion.
Stop the healthy daemon before changing the fixture, then arm the next daemon
with `PROVENANCE_CONTEXT_TEST_CRASH_ARMED=1` and
`PROVENANCE_CONTEXT_CRASH_FAILPOINT=<name>`. Append a distinct post marker for
insert phases; replace or truncate the source with a shorter post record for
delete/assertion phases; rename it after appending the post marker for rename
phases. To test explicit deletion, remove the synthetic source, read its
redacted `source_id` from status, and use the public `erase` command. Confirm
the armed daemon exits 86, unset both variables, restart normally, and verify
the prior and post markers are cited exactly once.

The 13 advertised crash boundaries are: `after_event_insert`,
`after_fts_insert`, `after_assertion_supersede`, `after_assertion_insert`,
`after_delete_assertions`, `after_delete_fts`, `after_delete_events`,
`after_source_state`, `after_meta_counts`, `before_commit`,
`after_rename_events`, `after_rename_source_state`, and
`before_rename_commit`.

## Quality commands

Runtime tests use `/opt/homebrew/bin/python3.13`. Black, Ruff, and Pyright use
the installed executables below; they are not installed into that interpreter.

```sh
/Users/garyharr/Library/Python/3.9/bin/black --check scripts hooks tests
/Users/garyharr/Library/Python/3.9/bin/ruff check scripts hooks tests
/Users/garyharr/Library/Python/3.9/bin/pyright
/opt/homebrew/bin/python3.13 -m unittest discover -s tests -v
```

## launchd template

`launchd/com.provenance-context.plist.template` pins the same
`/opt/homebrew/bin/python3.13` used by hooks. This repository never bootstraps
a personal service. Copy it to
`~/Library/LaunchAgents/com.provenance-context.plist`, replace each
`__UPPERCASE__` value with an absolute path, then validate and control it:

```sh
mkdir -p "$HOME/.local/share/provenance-context"
chmod 700 "$HOME/.local/share/provenance-context"
plutil -lint ~/Library/LaunchAgents/com.provenance-context.plist
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.provenance-context.plist
launchctl print "gui/$(id -u)/com.provenance-context"
```

The template uses `KeepAlive`; after login, crash, or wake the daemon performs
the same reconciliation. `launchctl bootout` is the explicit reversible stop.

The old `build --sessions-root --db` and `recall --db` commands remain only for
one-off archive migration and compatibility checks. They are not the service
path and no hook invokes them.
