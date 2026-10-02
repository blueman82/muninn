"""Names, paths and limits shared by every installer module."""

from __future__ import annotations

import os

LABEL = "com.provenance-context"
MKT_NAME = "provenance-context-local"
PLUGIN_ID = f"provenance-context@{MKT_NAME}"
MARKETPLACE = f"[marketplaces.{MKT_NAME}]"
PLUGIN = f'[plugins."{PLUGIN_ID}"]'
EVENTS = ("pre_tool_use", "session_start", "user_prompt_submit")
# Codex keys a hook's trust record by plugin, file, event and position; we
# only ever manage the first group and handler (``:0:0``) of each event.
TRUST = {
    e: f'[hooks.state."{PLUGIN_ID}:hooks/hooks.json:{e}:0:0"]' for e in EVENTS
}
# The only keys we may read or write in each of our config.toml sections.
CODEX_KEYS = {
    MARKETPLACE: {"source_type", "source", "last_updated", "last_revision"},
    PLUGIN: {"enabled"},
    **{h: {"enabled", "trusted_hash"} for h in TRUST.values()},
}
# Substrings that mark a line as ours; none may appear outside our sections.
MARKERS = (MKT_NAME, "provenance-context@")
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit")
# Codex versions whose trust-hash algorithm we have checked against both the
# source and a live ``hooks/list`` answer. Any other version leaves trusting
# the hooks to the owner instead of writing a hash that might not match.
CODEX_VERIFIED = ("codex-cli 0.159.2", "codex-cli 0.159.3")
PLIST = "Library/LaunchAgents/com.provenance-context.plist"
# Longest age of status.json that still counts as a live heartbeat.
HEARTBEAT_S = 120
# Files read from git at the pinned commit, never from the working tree. The
# first entry is the Claude hook fragment; preflight relies on that order.
PINNED = (
    "integrations/claude/settings-hooks.json",
    "integrations/codex/.agents/plugins/marketplace.json",
    "integrations/codex/.codex-plugin/plugin.json",
    "integrations/codex/hooks/hooks.json",
    "launchd/com.provenance-context.plist",
)
OWNER_STEP = (
    "OWNER STEP: start a new Codex session, run /hooks and trust the two "
    "provenance-context hooks (session_start, user_prompt_submit)."
)
# Read-only git calls must not take index.lock, or they would race the
# owner's own git commands in the same repository.
GIT_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
