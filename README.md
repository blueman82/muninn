# Provenance Context

Local, source-cited session evidence for Codex and Claude Code. Historical
evidence is untrusted data, never instructions.

```sh
export PROVENANCE_CONTEXT_DB=\
  "$HOME/.local/share/provenance-context/context.sqlite"
python3 scripts/context.py build \
  --sessions-root "$HOME/.codex/sessions" \
  --db "$PROVENANCE_CONTEXT_DB"
```

Use the same `PROVENANCE_CONTEXT_DB` value in both provider sessions. The
database is rebuilt from immutable JSONL; it is not a shared daemon.

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
  --db "$PROVENANCE_CONTEXT_DB" \
  --prompt 'why did this fail?' \
  --repo /path/to/repo
python3 scripts/claude_context.py \
  --db "$PROVENANCE_CONTEXT_DB" \
  --prompt 'why did this fail?' \
  --repo /path/to/repo
```
