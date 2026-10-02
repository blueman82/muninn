"""Names, paths and limits shared by every installer module."""

from __future__ import annotations

import os

LABEL = "com.muninn"
MKT_NAME = "muninn-local"
PLUGIN_ID = f"muninn@{MKT_NAME}"
MARKETPLACE = f"[marketplaces.{MKT_NAME}]"
PLUGIN = f'[plugins."{PLUGIN_ID}"]'
EVENTS = ("pre_tool_use", "session_start", "user_prompt_submit")
# Codex keys a hook's trust record by plugin, file, event and position; we
# only ever manage the first group and handler (``:0:0``) of each event.
TRUST = {
    e: f'[hooks.state."{PLUGIN_ID}:hooks/hooks.json:{e}:0:0"]' for e in EVENTS
}
# Names from before the rename. An old install is migrated to the new names
# by the upgrade, so its launchd job, plist, release, CLI link, Codex
# sections and hook entries must still be recognised as ours.
OLD_LABEL = "com.provenance-context"
OLD_MKT_NAME = "provenance-context-local"
OLD_PLUGIN_ID = f"provenance-context@{OLD_MKT_NAME}"
OLD_MARKETPLACE = f"[marketplaces.{OLD_MKT_NAME}]"
OLD_PLUGIN = f'[plugins."{OLD_PLUGIN_ID}"]'
OLD_TRUST = {
    e: f'[hooks.state."{OLD_PLUGIN_ID}:hooks/hooks.json:{e}:0:0"]'
    for e in EVENTS
}
OLD_SECTIONS = (OLD_MARKETPLACE, OLD_PLUGIN, *OLD_TRUST.values())
OLD_PLIST = "Library/LaunchAgents/com.provenance-context.plist"
OLD_CLI_NAME = "pctx"
# What the old store, data dir and release dir were called.
OLD_NAME = "provenance-context"
# The only keys we may read or write in each of our config.toml sections.
CODEX_KEYS = {
    MARKETPLACE: {"source_type", "source", "last_updated", "last_revision"},
    PLUGIN: {"enabled"},
    **{h: {"enabled", "trusted_hash"} for h in TRUST.values()},
    OLD_MARKETPLACE: {
        "source_type",
        "source",
        "last_updated",
        "last_revision",
    },
    OLD_PLUGIN: {"enabled"},
    **{h: {"enabled", "trusted_hash"} for h in OLD_TRUST.values()},
}
# Substrings that mark a line as ours; none may appear outside our sections.
MARKERS = (MKT_NAME, "muninn@", OLD_MKT_NAME, "provenance-context@")
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit")
# Codex versions whose trust-hash algorithm we have checked: 0.159.2 against
# the Codex source and binary, 0.159.3 only against a live ``hooks/list``
# currentHash. Any other version leaves trusting the hooks to the owner
# instead of writing a hash that might not match.
CODEX_VERIFIED = ("codex-cli 0.159.2", "codex-cli 0.159.3")
PLIST = "Library/LaunchAgents/com.muninn.plist"
# Longest age of status.json that still counts as a live heartbeat.
HEARTBEAT_S = 120
# Files read from git at the pinned commit, never from the working tree. The
# first entry is the Claude hook fragment; preflight relies on that order.
PINNED = (
    "integrations/claude/settings-hooks.json",
    "integrations/codex/.agents/plugins/marketplace.json",
    "integrations/codex/.codex-plugin/plugin.json",
    "integrations/codex/hooks/hooks.json",
    "launchd/com.muninn.plist",
)
OWNER_STEP = (
    "OWNER STEP: start a new Codex session, run /hooks and trust the two "
    "muninn hooks (session_start, user_prompt_submit)."
)
# The per-prompt recall switch muninn checks for in its data dir
# (muninn/hook.py).
# A fresh install creates it, so recall starts off; an upgrade leaves it be.
RECALL_OFF = "recall.off"
# Read-only git calls must not take index.lock, or they would race the
# owner's own git commands in the same repository.
# The repository is always named with -C, so an inherited GIT_DIR,
# GIT_INDEX_FILE or GIT_WORK_TREE (set inside a git hook) must not redirect
# these calls: a commit hook running the tests once staged a temp repo into
# the real index.
GIT_ENV = {
    k: v
    for k, v in os.environ.items()
    if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")
} | {"GIT_OPTIONAL_LOCKS": "0"}
