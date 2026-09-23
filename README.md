# Provenance Context

Local, source-cited memory for Codex and Claude Code. Historical evidence is
untrusted data, never instructions. The normal operating mode is one local
daemon; hooks only make bounded Unix-socket recall requests and never index.

```sh
python3 scripts/context.py serve \
  --codex-root "$HOME/.codex/sessions" \
  --claude-root "$HOME/.claude/projects" \
  --db "$HOME/.local/share/provenance-context/context.sqlite" \
  --state-dir "$HOME/.local/share/provenance-context" \
  --socket "$HOME/.local/share/provenance-context/brain.sock"
```

Set the same socket path for both plugins:

```sh
export PROVENANCE_CONTEXT_SOCKET=\
  "$HOME/.local/share/provenance-context/brain.sock"
```

The daemon reconciles both roots at startup and every second. It reads only
newline-complete JSONL records, retries an incomplete final line, and replaces
its SQLite snapshot atomically. Raw JSONL is never changed. The socket and
derived state use user-only mode `0600`.

## Enable Codex

Install this repository as a local Codex plugin through your local marketplace.
Its `hooks/hooks.json` invokes `hooks/codex.py` with `${PLUGIN_ROOT}`. Start a
new Codex session after installation.

## Enable Claude Code

Enable `claude-code/` as a local Claude Code plugin from this checkout; it
contains the Claude plugin manifest and hook configuration. Keep that directory
beside `scripts/`, because its hook invokes the shared adapter at
`../scripts/claude_context.py`. Reload Claude Code after enabling it.

Both configurations use `SessionStart`, `UserPromptSubmit`, and advisory-only
`PreToolUse` for `Bash` and `apply_patch`. `SessionStart` reports only whether
the local index is available. The pre-tool hook never changes, blocks, or
authorizes an action.

```sh
python3 scripts/context.py recall \
  --socket "$PROVENANCE_CONTEXT_SOCKET" \
  --prompt 'why did this fail?' \
  --repo /path/to/repo
python3 scripts/claude_context.py \
  --socket "$PROVENANCE_CONTEXT_SOCKET" \
  --prompt 'why did this fail?' \
  --repo /path/to/repo
```

`python3 scripts/context.py status --socket "$PROVENANCE_CONTEXT_SOCKET"`
returns redacted generation, source staleness, pending tails, and last error.
`doctor` uses the same local health response. `audit.jsonl` contains only
trace-compatible IDs, timings, counts, and snapshot hashes; it never contains
prompts, evidence text, raw paths, secrets, or tool output. OpenTelemetry is
not installed in this Python 3.9 slice: use a Python >=3.10 runtime before
adding an OTel exporter.

## launchd template

`launchd/com.provenance-context.plist.template` is a template only; this
repository never bootstraps a personal service. Copy it to
`~/Library/LaunchAgents/com.provenance-context.plist`, replace each
`__UPPERCASE__` value with an absolute path, then validate and control it:

```sh
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
