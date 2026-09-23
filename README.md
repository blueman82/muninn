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
`doctor` uses the same local health response. `audit.jsonl` contains only
trace-compatible IDs, timings, counts, and snapshot hashes; it never contains
prompts, evidence text, raw paths, secrets, or tool output. OpenTelemetry is
not installed in this Python 3.13 slice; add it only with independently tested
instrumentation. Pattern filtering is defense in depth, not proof every secret
representation is detected. It suppresses known GitHub tokens and URLs with
userinfo or credential query parameters; ordinary URLs remain eligible
evidence.

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
